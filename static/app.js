// MeroShare Auto-Apply. Front-end logic.
//
// Architecture: a single Store keeps every server-derived data source
// in memory with a per-source TTL. Tab navigation reads from the Store
// instead of refetching, so flipping between Dashboard / History /
// Settings doesn't trigger a thundering herd of network calls. The
// header shows a per-tab "synced Xs ago" indicator so users always
// know how fresh the page is.

(() => {
  // ── Helpers ─────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);
  const el = (tag, attrs = {}, children = []) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') e.className = v;
      else if (k === 'style') e.style.cssText = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v !== null && v !== undefined) e.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c == null) continue;
      e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return e;
  };

  const initials = (name) => {
    const parts = String(name || '?').trim().split(/\s+/);
    return ((parts[0]?.[0] || '?') + (parts[1]?.[0] || '')).toUpperCase();
  };

  function relTime(iso) {
    if (!iso) return null;
    const t = new Date(String(iso).replace(' ', 'T')).getTime();
    if (Number.isNaN(t)) return null;
    const diffMs = t - Date.now();
    const past = diffMs < 0;
    const mins = Math.round(Math.abs(diffMs) / 60000);
    if (mins < 1) return past ? 'just now' : 'in <1m';
    if (mins < 60) return past ? `${mins}m ago` : `in ${mins}m`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return past ? `${hrs}h ago` : `in ${hrs}h`;
    const days = Math.round(hrs / 24);
    return past ? `${days}d ago` : `in ${days}d`;
  }

  function relTimeMs(ms) {
    if (!ms) return null;
    return relTime(new Date(ms).toISOString());
  }

  // ── Store ──────────────────────────────────────────────────────
  const Store = {
    state: {
      // _pendingForce: see fetchOnce. Stores a "user clicked refresh
      // while a request was in flight" marker so we can fire the
      // forced fetch as soon as the in-flight one resolves.
      issues:    { data: null, fetchedAt: null, error: null, loading: false, _pendingForce: false, ttl: 60_000,      url: '/api/issues' },
      // History requires a per-account login + N detail fetches.
      // Marked lazy so the dashboard's initial sync doesn't pile auth
      // calls on top of /api/issues. Loaded when the History tab is
      // first activated.
      history:   { data: null, fetchedAt: null, error: null, loading: false, ttl: 5 * 60_000,  url: '/api/status', lazy: true },
      accounts:  { data: null, fetchedAt: null, error: null, loading: false, ttl: 5 * 60_000,  url: '/api/accounts' },
      config:    { data: null, fetchedAt: null, error: null, loading: false, ttl: 5 * 60_000,  url: '/api/config' },
      scheduler: { data: null, fetchedAt: null, error: null, loading: false, ttl: 30_000,      url: '/api/scheduler' },
      logs:      { data: null, fetchedAt: null, error: null, loading: false, ttl: 10_000,      url: '/api/logs' },
      // Local "we already applied" cache. Loaded lazily on the
      // History tab so the user can clear a stale entry when our
      // record disagrees with MeroShare's.
      applied:   { data: null, fetchedAt: null, error: null, loading: false, ttl: 60_000,     url: '/api/applied-issues', lazy: true },
    },
    subs: {},

    subscribe(key, cb) {
      (this.subs[key] = this.subs[key] || []).push(cb);
      cb(this.state[key]);
    },

    notify(key) {
      (this.subs[key] || []).forEach((cb) => cb(this.state[key]));
    },

    set(key, patch) {
      Object.assign(this.state[key], patch);
      this.notify(key);
    },

    isFresh(key) {
      const s = this.state[key];
      return s.fetchedAt && (Date.now() - s.fetchedAt) < s.ttl;
    },

    async fetchOnce(key, { force = false } = {}) {
      const s = this.state[key];
      // If a fetch is in flight and the caller wants forced-fresh
      // data (e.g. user just clicked refresh), schedule a follow-up
      // fetch when the current one resolves. Without this, a refresh
      // button click during a slow /api/issues was silently dropped.
      if (s.loading) {
        if (force) s._pendingForce = true;
        return;
      }
      if (!force && this.isFresh(key)) return;

      this.set(key, { loading: true, error: null });
      const url = key === 'issues' && force ? `${s.url}?force=true` : s.url;
      try {
        const res = await fetch(url);
        const body = await res.json().catch(() => null);
        if (!res.ok) {
          this.set(key, { loading: false, error: (body && body.error) || `HTTP ${res.status}` });
          return;
        }
        if (body && body.error) {
          this.set(key, { loading: false, error: body.error });
          return;
        }
        this.set(key, { loading: false, data: body, fetchedAt: Date.now() });
      } catch (e) {
        this.set(key, { loading: false, error: 'Network error. Server may be stopped.' });
      } finally {
        // If a forced-refresh request was queued while this fetch
        // was in flight, fire it now. The forced fetch skips the
        // TTL check so the user gets their fresh data immediately.
        if (s._pendingForce) {
          s._pendingForce = false;
          this.fetchOnce(key, { force: true });
        }
      }
    },

    invalidate(key) {
      this.set(key, { fetchedAt: null });
    },
  };

  // ── Toasts (with deduplication + click-to-dismiss) ─────────────
  const toastsLive = new Map(); // text -> { el, timer }

  function toast(msg, type = 'success') {
    if (!msg) return;
    const stack = $('#toastStack');
    if (toastsLive.has(msg)) {
      // Already showing. Restart its timer rather than stacking duplicates.
      clearTimeout(toastsLive.get(msg).timer);
      toastsLive.get(msg).timer = setTimeout(() => removeToast(msg), 4500);
      return;
    }
    const t = el('div', {
      class: `toast toast-${type}`,
      text: msg,
      onClick: () => removeToast(msg),  // click anywhere on the toast
    });
    stack.appendChild(t);
    const timer = setTimeout(() => removeToast(msg), 4500);
    toastsLive.set(msg, { el: t, timer });
  }

  function removeToast(msg) {
    const entry = toastsLive.get(msg);
    if (!entry) return;
    clearTimeout(entry.timer);
    entry.el.remove();
    toastsLive.delete(msg);
  }

  // ── Modal ──────────────────────────────────────────────────────
  function modal({ title, body, confirmLabel = 'Confirm', confirmVariant = 'btn-primary' }) {
    return new Promise((resolve) => {
      // Stable id per modal so aria-labelledby on the dialog points
      // at the actual title element. Without role=dialog +
      // aria-modal=true + aria-labelledby, screen readers don't
      // announce these as dialogs and may continue reading background
      // content despite our focus trap.
      const titleId = 'modal-title-' + Math.random().toString(36).slice(2, 9);
      const cancel = el('button', { type: 'button', class: 'btn btn-outline', text: 'Cancel', onClick: () => done(false) });
      const ok = el('button', { type: 'button', class: `btn ${confirmVariant}`, text: confirmLabel, onClick: () => done(true) });
      // Body can be a single string or one with `\n\n` paragraph
      // breaks. Split into separate <p> tags so multi-line dialogs
      // (e.g. apply confirm with cost preview line) render with real
      // visual separation rather than collapsing into one wall of text.
      const paragraphs = String(body || '').split(/\n\s*\n/).filter(Boolean);
      const card = el('div', {
        class: 'modal',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-labelledby': titleId,
      }, [
        el('h3', { id: titleId, text: title }),
        ...paragraphs.map((p) => el('p', { text: p })),
        el('div', { class: 'modal-actions' }, [cancel, ok]),
      ]);
      const backdrop = el('div', { class: 'modal-backdrop', onClick: (e) => { if (e.target === backdrop) done(false); } }, [card]);
      const previouslyFocused = document.activeElement;
      function done(v) {
        backdrop.remove();
        document.body.classList.remove('modal-open');
        document.removeEventListener('keydown', onKey);
        if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
          previouslyFocused.focus();
        }
        resolve(v);
      }
      function onKey(e) {
        if (e.key === 'Escape') done(false);
        if (e.key === 'Enter') done(true);
        // Focus trap. Keep Tab and Shift+Tab inside the modal.
        if (e.key === 'Tab') {
          const focusables = card.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
          );
          if (!focusables.length) return;
          const first = focusables[0];
          const last = focusables[focusables.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
      }
      document.addEventListener('keydown', onKey);
      document.body.classList.add('modal-open');
      document.body.appendChild(backdrop);
      ok.focus();
    });
  }

  // ── Skeleton helpers ───────────────────────────────────────────
  function skeletonRows(n) {
    const wrap = el('div');
    for (let i = 0; i < n; i++) {
      wrap.appendChild(el('div', { class: 'skeleton-row' }, [
        el('div', { class: 'skeleton skeleton-block', style: 'flex:1;' }),
        el('div', { class: 'skeleton skeleton-block', style: 'width:80px;' }),
      ]));
    }
    return wrap;
  }

  function emptyState(title, body) {
    return el('div', { class: 'empty-state' }, [
      el('h3', { text: title }),
      el('p', { text: body || '' }),
    ]);
  }

  function errorState(message) {
    return el('div', { class: 'error-state', text: message });
  }

  // ── Sync indicator (aggregate across all sources) ─────────────
  // The header reflects the *whole app*'s sync state, not a single tab's
  //. That way switching tabs doesn't flicker the indicator and the user
  // always knows how fresh the data is regardless of where they are.
  let activeTab = 'dashboard';

  function aggregateSync() {
    const states = Object.values(Store.state);
    const total = states.length;
    const loading = states.filter((s) => s.loading).length;
    const loaded = states.filter((s) => s.fetchedAt !== null).length;
    const errors = states.filter((s) => s.error !== null).length;

    if (loading > 0 && loaded < total) {
      return { state: 'loading', label: total > loaded ? `syncing… (${loaded}/${total})` : 'syncing…' };
    }
    if (loaded === 0) {
      return errors > 0
        ? { state: 'error', label: 'sync failed' }
        : { state: 'stale', label: 'not synced' };
    }
    const ts = Math.min(...states.filter((s) => s.fetchedAt).map((s) => s.fetchedAt));
    const tail = errors > 0 ? ` · ${errors} failed` : '';
    return {
      state: errors > 0 ? 'stale' : 'fresh',
      label: `synced ${relTimeMs(ts)}${tail}`,
    };
  }

  function renderSyncMeta() {
    const agg = aggregateSync();
    const dot = $('#syncDot');
    const text = $('#syncText');
    const btn = $('#refreshBtn');
    btn.classList.toggle('spinning', agg.state === 'loading');
    btn.disabled = agg.state === 'loading';
    dot.className = 'dot' + (agg.state === 'loading' ? ' loading'
                          :  agg.state === 'error'   ? ' error'
                          :  agg.state === 'stale'   ? ' stale' : '');
    text.textContent = agg.label;
  }

  // ── Renderers ──────────────────────────────────────────────────
  function categoryToTag(cat) {
    if (cat === 'right_share') return { cls: 'tag-right', label: 'RIGHT SHARE', tip: 'Reserved for existing shareholders. Per-account eligibility differs.' };
    if (cat === 'fpo')         return { cls: 'tag-fpo',   label: 'FPO',         tip: 'Further Public Offering. Additional shares from a listed company.' };
    if (cat === 'mutual_fund') return { cls: 'tag-mf',    label: 'MUTUAL FUND', tip: 'Mutual fund scheme. Units, not shares.' };
    if (cat === 'debenture')   return { cls: 'tag-deb',   label: 'DEBENTURE',   tip: 'Bond / debenture. Fixed-income, not equity.' };
    return { cls: 'tag-ipo', label: 'IPO', tip: 'Initial Public Offering. Primary market issue, open to all.' };
  }

  // Parse MeroShare's date formats into a JS Date or null. The server
  // sends issueCloseDate verbatim from MeroShare, which has used both
  // "2026-05-04 02:30:00" and "2026/05/04 02:30:00" historically. Naive
  // strings are interpreted as Asia/Kathmandu (where MeroShare runs) so
  // a user on a non-NPT machine doesn't see a 12-hour shift on the
  // urgency calculation.
  function parseMeroshareDate(s) {
    if (!s) return null;
    const trimmed = String(s).trim().replace(/\//g, '-').replace(' ', 'T');
    // Naive form: append +05:45 (Nepal) before parsing.
    const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(trimmed);
    const withTz = hasTz ? trimmed : trimmed + '+05:45';
    const d = new Date(withTz);
    return isNaN(d.getTime()) ? null : d;
  }

  // Returns {label, cls} for a "Closes in X" badge or null when the
  // close date is unknown / already passed (passed dates the server
  // wouldn't normally include — they're filtered upstream).
  function urgencyBadge(closeDate) {
    if (!closeDate) return null;
    const d = parseMeroshareDate(closeDate);
    if (!d) return null;
    const msLeft = d.getTime() - Date.now();
    if (msLeft <= 0) return null;
    const hoursLeft = msLeft / (1000 * 60 * 60);
    let label, cls;
    if (hoursLeft <= 24) {
      const hh = Math.max(1, Math.round(hoursLeft));
      label = `Closes in ${hh}h`;
      cls = 'tag-urgent';
    } else if (hoursLeft <= 72) {
      const dd = Math.ceil(hoursLeft / 24);
      label = `Closes in ${dd} day${dd === 1 ? '' : 's'}`;
      cls = 'tag-warn';
    } else {
      const dd = Math.ceil(hoursLeft / 24);
      label = `Closes in ${dd} days`;
      cls = 'tag-info';
    }
    return { label, cls, tip: `Issue closes ${d.toLocaleString()}` };
  }

  function renderIssues(s) {
    const host = $('#issuesList');
    const countEl = $('#issuesCount');
    if (countEl) {
      const total = (s.data || []).length;
      let pending = 0;
      for (const i of s.data || []) {
        for (const a of Object.values(i.applications || {})) if (!a.applied) pending++;
      }
      countEl.textContent = total === 0 ? '' : `${total} open · ${pending} pending`;
    }
    host.replaceChildren();
    if (s.loading && !s.data) { host.appendChild(skeletonRows(3)); return; }
    if (s.error) { host.appendChild(errorState(s.error)); return; }
    if (!s.data || !s.data.length) {
      // Empty-state hint: include the next scheduler run time so the
      // user knows when the next automatic check will pick up new issues.
      const sched = Store.state.scheduler.data || {};
      const subline = sched.enabled && sched.next_run
        ? `Next auto-check ${relTime(sched.next_run)}.`
        : sched.enabled
          ? 'Background scheduler is on; first check pending.'
          : 'Background scheduler is off. Hit refresh anytime.';
      host.appendChild(emptyState('No open issues', subline));
      return;
    }

    // Sort most-urgent first, then alphabetical. Issues without a
    // close date sink to the bottom: typically locally-recorded
    // historic applications surfaced for visibility, not actionable.
    const sorted = (s.data || []).slice().sort((a, b) => {
      const da = parseMeroshareDate(a.issueCloseDate);
      const db = parseMeroshareDate(b.issueCloseDate);
      if (da && db) return da.getTime() - db.getTime();
      if (da && !db) return -1;
      if (!da && db) return 1;
      return (a.company || '').localeCompare(b.company || '');
    });
    for (const issue of sorted) {
      const cat = categoryToTag(issue.category);
      const tags = el('div', { class: 'issue-tags' }, [
        el('span', { class: `tag ${cat.cls}`, text: cat.label, title: cat.tip }),
      ]);
      const urgency = urgencyBadge(issue.issueCloseDate);
      if (urgency) {
        tags.appendChild(el('span', {
          class: `tag ${urgency.cls}`, text: urgency.label, title: urgency.tip,
        }));
      }
      if (issue.shareGroup) tags.appendChild(el('span', { class: 'tag tag-neutral', text: issue.shareGroup }));

      // Optional detail row: close date, scrip, etc. (server may or may
      // not include these. Fields are added defensively).
      const detail = el('div', { class: 'issue-detail' });
      if (issue.scrip) detail.appendChild(el('span', { text: issue.scrip }));
      if (issue.shareType && issue.shareType !== '?') detail.appendChild(el('span', { text: issue.shareType }));
      if (issue.reservation) detail.appendChild(el('span', { text: issue.reservation }));

      const apps = issue.applications || {};
      const chips = el('div', { class: 'acct-chips' });
      const pendingIds = [];
      for (const [acctId, a] of Object.entries(apps)) {
        let cls = 'chip';
        if (a.applied) cls += ' applied';
        else if (a.stateUnknown) cls += ' unknown';
        chips.appendChild(el('span', { class: cls, title: a.stateUnknown ? 'Server state unknown for this account' : '' }, [
          el('span', { class: 'avatar', text: initials(a.accountName) }),
          el('span', { text: a.accountName }),
        ]));
        if (!a.applied) pendingIds.push(acctId);
      }

      const info = el('div', { class: 'issue-info' }, [
        el('div', { class: 'issue-name', text: issue.company }),
        tags,
      ]);
      if (detail.children.length) info.appendChild(detail);
      info.appendChild(chips);

      let actionBtn;
      if (pendingIds.length === 0) {
        actionBtn = el('button', { class: 'btn btn-success', disabled: '', text: 'All applied' });
      } else {
        const label = pendingIds.length === 1 ? 'Apply' : `Apply all (${pendingIds.length})`;
        actionBtn = el('button', {
          class: 'btn btn-primary',
          text: label,
        });
        actionBtn.addEventListener('click', () => {
          // Pass the button ref through so applyForIssue can flip it
          // to a disabled "Applying…" state immediately on confirm,
          // closing the visual gap between click and the next /api/issues
          // refresh (which re-renders the row a few seconds later).
          applyForIssue(issue.id, issue.company, pendingIds, issue.category, actionBtn);
        });
      }
      host.appendChild(el('div', {
        class: 'issue-row',
        'data-issue-id': String(issue.id),
      }, [info, actionBtn]));
    }
  }

  // History filters. Persisted in module state so toolbar interactions
  // don't lose state on a Store re-render.
  const historyFilters = { account: 'ALL', status: 'ALL', q: '' };

  function renderHistoryToolbar() {
    // Rebuild only the account-side buttons from the current accounts list.
    const accts = Store.state.accounts.data || [];
    const toolbar = $('#historyToolbar');
    if (!toolbar) return;

    // Strip prior account buttons (keep status buttons).
    toolbar.querySelectorAll('button[data-filter="account"]').forEach((b) => b.remove());

    // Insert account buttons before the first separator.
    const sep = toolbar.querySelector('span');
    const allBtn = el('button', {
      class: 'filter-btn' + (historyFilters.account === 'ALL' ? ' active' : ''),
      text: 'All accounts', 'data-filter': 'account', 'data-value': 'ALL',
      onClick: () => setHistoryFilter('account', 'ALL'),
    });
    toolbar.insertBefore(allBtn, sep);
    for (const a of accts) {
      toolbar.insertBefore(el('button', {
        class: 'filter-btn' + (historyFilters.account === a.id ? ' active' : ''),
        text: a.name, 'data-filter': 'account', 'data-value': a.id,
        onClick: () => setHistoryFilter('account', a.id),
      }), sep);
    }

    // Wire status buttons (idempotent).
    toolbar.querySelectorAll('button[data-filter="status"]').forEach((b) => {
      const on = historyFilters.status === b.dataset.value;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.onclick = () => setHistoryFilter('status', b.dataset.value);
    });
    // Sync aria-pressed on the dynamically-inserted account buttons too.
    toolbar.querySelectorAll('button[data-filter="account"]').forEach((b) => {
      const on = historyFilters.account === b.dataset.value;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function setHistoryFilter(kind, value) {
    historyFilters[kind] = value;
    renderHistoryToolbar();
    renderHistory(Store.state.history);
  }

  // Memoize the sorted-by-date snapshot so a keystroke in the search
  // box doesn't re-sort the whole history every render. Keyed on the
  // raw `s.data` reference. Invalidates automatically when /api/status
  // returns a new array.
  let _historySorted = { src: null, rows: null };
  function _getSortedHistory(srcData) {
    if (_historySorted.src === srcData) return _historySorted.rows;
    const rows = [...srcData].sort((a, b) => {
      const ta = new Date(a.appliedDate || 0).getTime();
      const tb = new Date(b.appliedDate || 0).getTime();
      return tb - ta;
    });
    _historySorted = { src: srcData, rows };
    return rows;
  }

  function renderHistory(s) {
    const host = $('#historyList');
    host.replaceChildren();
    if (s.loading && !s.data) { host.appendChild(skeletonRows(4)); return; }
    if (s.error) { host.appendChild(errorState(s.error)); return; }
    if (!s.data || !s.data.length) { host.appendChild(emptyState('No applications yet', 'Once you apply for an issue, it shows up here.')); return; }

    // Sort once per data update, then filter on every render.
    let filtered = _getSortedHistory(s.data);
    if (historyFilters.account !== 'ALL') {
      filtered = filtered.filter((r) => r.accountId === historyFilters.account);
    }
    if (historyFilters.status !== 'ALL') {
      filtered = filtered.filter((r) => (r.detailStatus || '') === historyFilters.status);
    }
    if (historyFilters.q) {
      const q = historyFilters.q.toLowerCase();
      filtered = filtered.filter((r) => (r.companyName || '').toLowerCase().includes(q));
    }

    const countEl = $('#historyCount');
    if (countEl) {
      const total = s.data.length;
      const shown = filtered.length;
      countEl.textContent = shown === total
        ? `${total}`
        : `${shown} of ${total}`;
    }

    if (!filtered.length) {
      host.appendChild(emptyState(
        'No matches',
        'Try a different filter, or click "All accounts / statuses".',
      ));
      return;
    }

    const rows = filtered;
    for (const r of rows) {
      const left = el('div', { style: 'flex:1; min-width:0;' });
      const acct = r.accountName ? `[${r.accountName}] ` : '';
      left.appendChild(el('div', { class: 'report-name', text: acct + (r.companyName || '?') }));
      const metaParts = [r.scrip, r.shareTypeName, r.shareGroupName].filter(Boolean);
      if (metaParts.length) left.appendChild(el('div', { class: 'report-meta', text: metaParts.join(' · ') }));
      if (r.appliedKitta || r.amount) {
        const parts = [];
        if (r.appliedKitta) parts.push(`${r.appliedKitta} kitta`);
        if (r.amount) parts.push(`Rs. ${Number(r.amount).toLocaleString()}`);
        left.appendChild(el('div', { class: 'report-amount', text: parts.join(' · ') }));
      }
      if (r.meroshareRemark) {
        left.appendChild(el('div', { class: 'report-meta', style: 'margin-top:4px;', text: r.meroshareRemark }));
      }

      const status = r.detailStatus || '';
      let badgeCls = 'badge-muted', badgeText = status || 'Applied';
      if (status === 'Alloted') { badgeCls = 'badge-success'; badgeText = 'Allotted'; }
      else if (status === 'Not Alloted') { badgeCls = 'badge-danger'; badgeText = 'Not Allotted'; }
      else if (status === 'Verified') { badgeCls = 'badge-pending'; badgeText = 'Pending'; }

      const right = el('div', { style: 'text-align:right;' }, [el('span', { class: `badge ${badgeCls}`, text: badgeText })]);
      host.appendChild(el('div', { class: 'report-row' }, [left, right]));
    }
  }

  function renderAppliedCache(s) {
    const host = $('#appliedCacheList');
    if (!host) return;
    host.replaceChildren();
    const accounts = s.data || {};
    const accts = Store.state.accounts.data || [];
    const acctNameById = Object.fromEntries(accts.map((a) => [a.id, a.name]));
    const knownAcctIds = new Set(accts.map((a) => a.id));

    // Group rows by account so we can offer a per-account "Forget all"
    // and call out orphaned entries (account deleted but cache rows
    // remain. They'd otherwise look like rows under a mystery id).
    const groups = new Map();
    for (const [accountId, issues] of Object.entries(accounts)) {
      const isOrphan = !knownAcctIds.has(accountId);
      const list = [];
      for (const [issueId, rec] of Object.entries(issues || {})) {
        list.push({ issueId, rec });
      }
      if (list.length) {
        list.sort((a, b) => (b.rec.applied_at || '').localeCompare(a.rec.applied_at || ''));
        groups.set(accountId, {
          name: acctNameById[accountId] || accountId,
          isOrphan,
          rows: list,
        });
      }
    }
    if (!groups.size) {
      host.appendChild(el('div', { class: 'empty-state', text: 'No locally-cached applications.' }));
      return;
    }
    // Render each account as its own block with a "Forget all" button.
    for (const [accountId, group] of groups) {
      const headerStyle = group.isOrphan
        ? 'flex:1;font-size:12px;font-weight:600;color:var(--danger);'
        : 'flex:1;font-size:12px;font-weight:600;color:var(--text-soft);';
      const header = el('div', {
        style: 'display:flex;align-items:center;gap:8px;margin:14px 0 6px;',
      }, [
        el('div', {
          style: headerStyle,
          text: group.isOrphan
            ? `${group.name} (orphaned. Account no longer exists)`
            : group.name,
        }),
        el('button', {
          type: 'button',
          class: 'btn btn-danger-outline btn-icon',
          text: `Forget all (${group.rows.length})`,
          title: 'Clear every cached "already applied" entry for this account',
          onClick: () => forgetAllForAccount(accountId, group.name, group.rows.length),
        }),
      ]);
      host.appendChild(header);
      for (const { issueId, rec } of group.rows) {
        // _parseAppliedAt returns NaN for unparseable strings, and
        // relTimeMs returns null on NaN. Fall back to the raw stamp
        // or '?' so the row never shows a literal "null".
        const ms = _parseAppliedAt(rec.applied_at);
        const rel = Number.isFinite(ms) ? relTimeMs(ms) : null;
        const stamp = rel || rec.applied_at || '?';
        const left = el('div', { style: 'flex:1;min-width:0;' }, [
          el('div', { class: 'report-name', text: rec.company || issueId }),
          el('div', { class: 'report-meta', text: `${rec.type || '?'} · applied ${stamp}` }),
          rec.message ? el('div', { class: 'report-meta', style: 'margin-top:2px;', text: rec.message }) : null,
        ].filter(Boolean));
        const delBtn = el('button', {
          type: 'button',
          class: 'btn btn-danger-outline btn-icon',
          text: 'Forget',
          title: 'Remove this entry from local cache so the bot will re-check it on the next run',
          onClick: () => deleteAppliedEntry(accountId, issueId, rec.company || issueId),
        });
        host.appendChild(el('div', { class: 'report-row' }, [left, delBtn]));
      }
    }
  }

  // Robustly parse the seeded applied_at field which can come from
  // either MeroShare's report (their own date format) or our own ISO
  // timestamp. Returns NaN-safe milliseconds so relTimeMs renders "?"
  // instead of "Invalid Date" when parsing fails.
  function _parseAppliedAt(s) {
    if (!s) return NaN;
    // ISO with seconds + offset → Date can parse directly.
    let t = new Date(String(s).replace(' ', 'T')).getTime();
    if (!Number.isNaN(t)) return t;
    return NaN;
  }

  async function forgetAllForAccount(accountId, label, count) {
    const ok = await modal({
      title: `Forget all ${count} entries for "${label}"?`,
      body: 'Clears every locally-cached "already applied" record for this account. The next scheduled check will recompute eligibility from MeroShare itself.',
      confirmLabel: `Forget all ${count}`,
      confirmVariant: 'btn-danger-outline',
    });
    if (!ok) return;
    try {
      const res = await fetch(
        `/api/applied-issues/${encodeURIComponent(accountId)}`,
        { method: 'DELETE' },
      );
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        toast((body && body.error) || `HTTP ${res.status}`, 'error');
        return;
      }
      toast(`Cleared ${count} entries`);
      Store.invalidate('applied');
      Store.fetchOnce('applied', { force: true });
    } catch {
      toast('Could not contact server', 'error');
    }
  }

  async function deleteAppliedEntry(accountId, issueId, label) {
    const ok = await modal({
      title: `Forget "${label}"?`,
      body: 'Removes the local "already applied" record. The next scheduled check will treat this issue as new (and may try to apply for it again if MeroShare lists it as applicable).',
      confirmLabel: 'Forget',
      confirmVariant: 'btn-danger-outline',
    });
    if (!ok) return;
    try {
      const res = await fetch(`/api/applied-issues/${encodeURIComponent(accountId)}/${encodeURIComponent(issueId)}`, { method: 'DELETE' });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        toast((body && body.error) || `HTTP ${res.status}`, 'error');
        return;
      }
      toast('Removed from local cache');
      Store.invalidate('applied');
      Store.fetchOnce('applied', { force: true });
    } catch {
      toast('Could not contact server', 'error');
    }
  }

  // {account_id: {ok: bool, name: string|null, ts: number}}
  // lastTestStatus is the per-account verified/expiry record: populated
  // when the user clicks "Test login" in Settings. We persist it to
  // localStorage so refreshes don't re-zero the warnings — re-running
  // every test on every page load would burn through MeroShare's
  // login-rate budget for no real benefit. Stale records (>14 days)
  // are quietly dropped.
  const _LAST_TEST_KEY = 'meroshare:last-test-status';
  const _LAST_TEST_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;
  const lastTestStatus = (() => {
    try {
      const raw = localStorage.getItem(_LAST_TEST_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return {};
      const now = Date.now();
      for (const [k, v] of Object.entries(parsed)) {
        if (!v || !v.ts || (now - v.ts) > _LAST_TEST_MAX_AGE_MS) {
          delete parsed[k];
        }
      }
      return parsed;
    } catch {
      return {};
    }
  })();
  function _persistTestStatus() {
    try { localStorage.setItem(_LAST_TEST_KEY, JSON.stringify(lastTestStatus)); }
    catch {}
  }
  // Days until a MeroShare expiry date, or null if unparseable / past.
  // MeroShare uses both "2026-05-04 02:30:00" and "2026/05/04 ..."
  // historically, so reuse parseMeroshareDate.
  function daysUntil(dateStr) {
    if (!dateStr) return null;
    const d = parseMeroshareDate(dateStr);
    if (!d) return null;
    return Math.ceil((d.getTime() - Date.now()) / (1000 * 60 * 60 * 24));
  }

  // Top-of-dashboard banner when any tested account has CRN/password
  // expiry within 30 days. Renders nothing when the test cache is
  // empty — first-time users without any "Test login" runs see no
  // misleading "all clear" state.
  function renderExpiryBanner() {
    const host = $('#expiryBanner');
    if (!host) return;
    host.replaceChildren();
    const accts = Store.state.accounts.data || [];
    const warnings = [];
    for (const a of accts) {
      const t = lastTestStatus[a.id];
      if (!t || !t.ok) continue;
      const accountDays = daysUntil(t.expiredDate);
      const passwordDays = daysUntil(t.passwordExpiryDate);
      const minDays = Math.min(
        accountDays === null ? Infinity : accountDays,
        passwordDays === null ? Infinity : passwordDays,
      );
      if (minDays === Infinity || minDays > 30) continue;
      const what = (accountDays !== null && accountDays <= 30 && passwordDays !== null && passwordDays <= 30)
        ? 'account access & password'
        : (accountDays !== null && accountDays <= 30 ? 'account access' : 'password');
      warnings.push({ name: a.name, days: minDays, what });
    }
    if (!warnings.length) return;
    warnings.sort((x, y) => x.days - y.days);
    const minDays = warnings[0].days;
    // Red <7d (treated as immediate-action), yellow otherwise. The
    // single-card layout keeps the dashboard scan-friendly even when
    // multiple accounts are about to expire.
    const isUrgent = minDays <= 7;
    const card = el('div', {
      class: 'card',
      style: `border-left: 3px solid ${isUrgent ? 'var(--danger)' : 'var(--warning)'}; background: ${isUrgent ? 'var(--danger-soft)' : 'var(--warning-soft)'};`,
    });
    card.appendChild(el('div', {
      class: 'card-title',
      style: `color: ${isUrgent ? 'var(--danger)' : 'var(--warning)'};`,
      text: isUrgent ? 'Action needed: credentials expiring' : 'Heads up: credentials expiring soon',
    }));
    const list = el('ul', { style: 'margin: 0; padding-left: 18px; color: var(--text-soft);' });
    for (const w of warnings) {
      const phrase = w.days <= 0 ? 'EXPIRED' : `expires in ${w.days}d`;
      list.appendChild(el('li', { text: `${w.name}: ${w.what} ${phrase}` }));
    }
    card.appendChild(list);
    card.appendChild(el('div', {
      style: 'margin-top: 8px; font-size: 12px; color: var(--muted);',
      text: 'Renew on MeroShare directly. Expired credentials will silently fail mid-cycle.',
    }));
    host.appendChild(card);
  }

  // Lifetime stats card on the dashboard. Computes counts from the
  // /api/status response so the user has a single proof-of-value glance:
  // "I've applied for N issues, won M, never missed a window." Empty
  // when no history exists yet.
  function renderStatsCard(s) {
    const host = $('#statsCard');
    if (!host) return;
    host.replaceChildren();
    const reports = (s && s.data) || [];
    if (!reports.length) return;
    let allotted = 0, notAllotted = 0, other = 0;
    for (const r of reports) {
      // Two status fields are present on each report:
      //   detailStatus = the per-form detail-fetch outcome
      //                  ("Alloted" / "Not Alloted" / "Refunded" /
      //                   "Pending" / etc.)
      //   statusName   = the list-view status (often a lifecycle
      //                  stage like "Closed" / "Approved" rather
      //                  than the allotment outcome).
      // detailStatus is the more authoritative source — we prefer
      // it when populated. The previous order silently mis-classified
      // settled applications as "pending" when only the list-view
      // status was available.
      //
      // Order of substring tests also matters: "NOT ALLOTED"
      // contains "alloted", so the negative phrase MUST be tested
      // first or rejections get silently counted as wins.
      const status = String(r.detailStatus || r.statusName || '').toLowerCase();
      if (status.includes('not alloted') || status.includes('not allotted')
          || status.includes('rejected') || status.includes('failed')
          || status.includes('refund')) {
        notAllotted++;
      } else if (status.includes('alloted') || status.includes('allotted')) {
        allotted++;
      } else {
        // Pending, in-process, approved-but-not-yet-allotted, or
        // any future MeroShare status string we don't recognize.
        // We bucket as "Other" rather than the misleading "Pending"
        // — most of these ARE pending, but some (e.g. "Closed")
        // mean the IPO closed without allotment data we could fetch.
        other++;
      }
    }
    // Distinct accounts represented in this slice — surfacing this
    // tells the user "the numbers below combine these accounts."
    const accountCount = new Set(
      reports.map((r) => r.accountId).filter(Boolean),
    ).size || 1;
    const total = reports.length;
    const stat = (label, value, color) => el('div', {
      style: 'flex: 1; min-width: 100px; text-align: center; padding: 8px;',
    }, [
      el('div', {
        style: `font-size: 22px; font-weight: 700; color: ${color || 'var(--text)'};`,
        text: String(value),
      }),
      el('div', {
        style: 'font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px;',
        text: label,
      }),
    ]);
    const subtitle = accountCount === 1
      ? 'Up to 20 most recent applications.'
      : `Up to 20 most recent per account, combined across ${accountCount} accounts.`;
    const card = el('div', { class: 'card' }, [
      el('div', { class: 'card-title', text: 'Recent applications' }),
      el('div', {
        style: 'font-size: 11px; color: var(--muted); margin-top: -6px; margin-bottom: 6px;',
        text: subtitle,
      }),
      el('div', {
        style: 'display: flex; flex-wrap: wrap; gap: 4px;',
      }, [
        stat('In view', total),
        stat('Allotted', allotted, 'var(--success)'),
        stat('Not allotted', notAllotted, 'var(--danger)'),
        stat('Other', other, 'var(--warning)'),
      ]),
    ]);
    host.appendChild(card);
  }

  // Loud banner when the scheduler is enabled but hasn't actually run
  // in too long — the worst kind of failure for this tool, because the
  // dashboard previously said "scheduler: enabled" while no checks
  // were happening. We use the configured interval as the yardstick:
  //   < 1.5x   = healthy, no banner
  //   1.5x..2.5x = "may be stuck", warning yellow
  //   ≥ 2.5x   = "almost certainly broken", danger red
  // When enabled but never-run (fresh install), we show an info banner
  // for the first run window so the user knows what to expect.
  function renderSchedulerHealth() {
    const host = $('#schedulerHealth');
    if (!host) return;
    host.replaceChildren();
    const sched = Store.state.scheduler.data || {};
    if (!sched.enabled) return;
    const interval = sched.interval_hours || 6;
    const lastRunMs = sched.last_run
      ? parseMeroshareDate(sched.last_run)?.getTime()
      : null;
    const intervalMs = interval * 60 * 60 * 1000;

    let level = 'ok';
    let title = '';
    let detail = '';
    if (!lastRunMs) {
      level = 'info';
      title = 'Scheduler enabled — first run pending';
      detail = `First check will fire within ${interval}h. If it doesn't, see Settings → Background scheduler for the launchd status.`;
    } else {
      const ageMs = Date.now() - lastRunMs;
      const ageH = ageMs / (1000 * 60 * 60);
      if (ageMs <= intervalMs * 1.5) {
        return;  // healthy, don't render
      }
      if (ageMs >= intervalMs * 2.5) {
        level = 'danger';
        title = 'Scheduler may be broken';
        detail = `Last successful check ran ${formatHoursAgo(ageH)}, but the configured interval is ${interval}h. The launchd agent may have unloaded, the venv may be missing, or MeroShare may have IP-blocked the host. Check Settings → Background scheduler.`;
      } else {
        level = 'warn';
        title = 'Scheduler running late';
        detail = `Last check was ${formatHoursAgo(ageH)} (interval is ${interval}h). One delayed cycle is usually fine — check again in an hour.`;
      }
    }
    const colors = {
      info:   { bg: 'rgba(96,165,250,0.10)', edge: '#60a5fa' },
      warn:   { bg: 'var(--warning-soft)',   edge: 'var(--warning)' },
      danger: { bg: 'var(--danger-soft)',    edge: 'var(--danger)' },
    }[level];
    const card = el('div', {
      class: 'card',
      style: `border-left: 3px solid ${colors.edge}; background: ${colors.bg};`,
    }, [
      el('div', {
        class: 'card-title',
        style: `color: ${colors.edge};`,
        text: title,
      }),
      el('div', { style: 'color: var(--text-soft); font-size: 13px;', text: detail }),
    ]);
    host.appendChild(card);
  }
  function formatHoursAgo(hours) {
    if (hours < 1) return `${Math.round(hours * 60)}m ago`;
    if (hours < 24) return `${Math.round(hours)}h ago`;
    const days = Math.floor(hours / 24);
    const rem = Math.round(hours % 24);
    return rem ? `${days}d ${rem}h ago` : `${days}d ago`;
  }

  function renderAccounts(s) {
    const host = $('#accountsList');
    host.replaceChildren();
    if (s.loading && !s.data) { host.appendChild(skeletonRows(2)); return; }
    if (s.error) { host.appendChild(errorState(s.error)); return; }
    if (!s.data || !s.data.length) { host.appendChild(emptyState('No accounts yet', 'Click "Add account" to get started.')); return; }

    for (const a of s.data) {
      const actions = el('div', { class: 'acct-actions' }, [
        el('button', { class: 'btn btn-outline btn-icon', text: 'Test', onClick: () => testAccountLogin(a.id, a.name) }),
        el('button', { class: 'btn btn-outline btn-icon', text: 'Edit', onClick: () => showAccountForm(a) }),
        el('button', { class: 'btn btn-danger-outline btn-icon', text: 'Delete', onClick: () => deleteAccount(a.id, a.name) }),
      ]);
      let meta = `DP ${a.dp_id} · BOID ${a.username}`;
      if (a.preferred_bank) meta += ` · bank: ${a.preferred_bank}`;
      if (a.preferred_bank_account) meta += ` (acct ${a.preferred_bank_account})`;
      if (a.default_kitta) meta += ` · kitta ${a.default_kitta}`;
      const t = lastTestStatus[a.id];
      // Include the BOID-from-server in the verified line: lets the
      // user spot a credentials-mix-up where the account record's
      // BOID disagrees with what MeroShare returned post-login.
      const verifiedLine = t && t.ok
        ? `${t.name || 'verified'}`
          + (t.demat ? ' · DEMAT ' + t.demat : '')
          + (t.boid && t.boid !== a.username ? ' · BOID mismatch: server says ' + t.boid : '')
          + ` · checked ${relTimeMs(t.ts)}`
        : t && !t.ok
          ? `login failed ${relTimeMs(t.ts)}`
          : null;
      // Build a separate expiry line so the visual weight matches the
      // financial weight: a CRN/PIN expiring during an IPO open window
      // is the kind of failure users only discover after missing it.
      let expiryLine = null;
      let expiryColor = null;
      if (t && t.ok) {
        const accountDays = daysUntil(t.expiredDate);
        const passwordDays = daysUntil(t.passwordExpiryDate);
        const parts = [];
        if (accountDays !== null) {
          if (accountDays <= 0) parts.push('Account access EXPIRED — renew on MeroShare');
          else if (accountDays <= 30) parts.push(`Account access expires in ${accountDays}d`);
        }
        if (passwordDays !== null) {
          if (passwordDays <= 0) parts.push('Password EXPIRED — change on MeroShare');
          else if (passwordDays <= 30) parts.push(`Password expires in ${passwordDays}d`);
        }
        if (parts.length) {
          expiryLine = parts.join(' · ');
          // Red below 7d (immediate action), warning yellow otherwise.
          const minDays = Math.min(
            accountDays === null ? Infinity : accountDays,
            passwordDays === null ? Infinity : passwordDays,
          );
          expiryColor = minDays <= 7 ? 'var(--danger)' : 'var(--warning)';
        }
      }

      const left = el('div', {}, [
        el('div', { class: 'acct-name', text: a.name }),
        el('div', { class: 'acct-meta', text: meta }),
      ]);
      if (verifiedLine) {
        left.appendChild(el('div', {
          class: 'acct-meta',
          style: 'margin-top:2px; color: ' + (t.ok ? 'var(--success)' : 'var(--danger)') + ';',
          text: verifiedLine,
        }));
      }
      if (expiryLine) {
        left.appendChild(el('div', {
          class: 'acct-meta',
          style: `margin-top:2px; color: ${expiryColor}; font-weight: 600;`,
          text: expiryLine,
        }));
      }
      host.appendChild(el('div', { class: 'acct-card' }, [
        el('div', { class: 'acct-info' }, [
          el('div', { class: 'acct-avatar', text: initials(a.name) }),
          left,
        ]),
        actions,
      ]));
    }
  }

  function renderConfig(s) {
    // Skip until the real /api/config response lands. The previous
    // `if (!cfg) return` was unreachable (cfg defaulted to `{}` which
    // is truthy), so first-paint flipped every checkbox to its falsy
    // default before the real config came in.
    if (!s.data) return;
    const cfg = s.data;
    const st = cfg.share_types || {};
    const aa = cfg.auto_apply || {};
    // Defaults: ipo_ordinary and right_share are ON by default. A
    // missing key means "not yet configured", treat as default. fpo,
    // mutual_fund, debenture default OFF. Use a small helper so the
    // rule is uniform across share types and a future toggle can't
    // silently default the wrong way.
    const stChecked = (key, dflt) =>
      st[key] === undefined ? dflt : !!st[key];
    setChecked('pref-ipo',     stChecked('ipo_ordinary', true));
    setChecked('pref-right',   stChecked('right_share', true));
    setChecked('pref-fpo',     stChecked('fpo', false));
    setChecked('pref-mf',      stChecked('mutual_fund', false));
    setChecked('pref-deb',     stChecked('debenture', false));
    // Use ?? so a stored 0 round-trips correctly (the previous `||`
    // path silently turned a 0 cap back into 100000 every render).
    setVal('pref-kitta',       aa.default_kitta ?? 10);
    setVal('pref-maxamt',      aa.max_amount ?? 100000);
    setChecked('pref-rightmax', aa.right_share_apply_max !== false);
  }

  function setChecked(id, v) { const e = $('#' + id); if (e) e.checked = !!v; }
  function setVal(id, v) { const e = $('#' + id); if (e && e.value !== String(v)) e.value = v; }

  function renderSchedulerStatus(s) {
    const data = s.data || {};
    const toggle = $('#sched-toggle');
    const interval = $('#sched-interval');
    const status = $('#sched-status');
    if (!toggle) return;
    if (s.error) { status.textContent = 'Error: ' + s.error; return; }
    toggle.checked = !!data.enabled;
    // Don't stomp the user's in-progress edit on every poll tick.
    // The scheduler endpoint refreshes every 30s; without this the
    // user types "12" and watches it revert to "6" mid-keystroke.
    if (data.interval_hours
        && document.activeElement !== interval
        && interval.value !== String(data.interval_hours)) {
      interval.value = String(data.interval_hours);
    }
    if (!data.enabled) { status.textContent = 'Inactive. No background checks scheduled.'; return; }
    const parts = ['Active'];
    if (data.last_run) {
      parts.push(`last ran ${relTime(data.last_run)}`);
      if (data.last_result) parts.push(data.last_result);
    } else {
      parts.push('running first check…');
    }
    if (data.next_run) parts.push(`next run ${relTime(data.next_run)}`);
    status.textContent = parts.join(' · ');
  }

  // Active log filter ("ALL", "ERROR", "WARNING", "INFO").
  let logFilter = 'ALL';

  function detectLogLevel(line) {
    // Backend log format is "%(asctime)s [%(levelname)s] %(message)s",
    // so the bracketed token is reliable.
    const m = line.match(/\[(ERROR|WARNING|INFO|DEBUG)\]/);
    return m ? m[1] : '';
  }

  function renderLogs(s) {
    const box = $('#logBox');
    // Preserve scroll position if the user is reading older lines -
    // only auto-scroll if they're already at the bottom.
    const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 4;

    box.replaceChildren();
    if (s.loading && !s.data) { box.textContent = 'Loading…'; return; }
    if (s.error) { box.textContent = 'Error: ' + s.error; return; }
    if (!s.data || !s.data.length) { box.textContent = 'No logs yet.'; return; }

    const lines = s.data;
    const filtered = logFilter === 'ALL'
      ? lines
      : lines.filter((ln) => detectLogLevel(ln) === logFilter);

    if (!filtered.length) {
      box.textContent = `No ${logFilter.toLowerCase()} entries in the last ${lines.length} lines.`;
      return;
    }

    for (const line of filtered) {
      const lvl = detectLogLevel(line);
      box.appendChild(el('span', {
        class: `log-line lvl-${lvl.toLowerCase() || 'info'}`,
        text: line + '\n',
      }));
    }
    if (wasAtBottom) box.scrollTop = box.scrollHeight;
  }

  function csvEscape(v) {
    const s = v == null ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  function exportHistoryCsv() {
    const s = Store.state.history;
    if (!s.data || !s.data.length) { toast('Nothing to export', 'info'); return; }
    // Apply current filters so the export matches what the user sees.
    let rows = s.data;
    if (historyFilters.account !== 'ALL') rows = rows.filter((r) => r.accountId === historyFilters.account);
    if (historyFilters.status !== 'ALL') rows = rows.filter((r) => (r.detailStatus || '') === historyFilters.status);
    if (historyFilters.q) {
      const q = historyFilters.q.toLowerCase();
      rows = rows.filter((r) => (r.companyName || '').toLowerCase().includes(q));
    }
    const headers = ['account', 'company', 'scrip', 'shareType', 'shareGroup',
                     'kitta', 'amount', 'appliedDate', 'status', 'remark'];
    const body = rows.map((r) => [
      r.accountName, r.companyName, r.scrip, r.shareTypeName, r.shareGroupName,
      r.appliedKitta, r.amount, r.appliedDate, r.detailStatus, r.meroshareRemark,
    ].map(csvEscape).join(','));
    const csv = [headers.join(','), ...body].join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    // Include time so multiple exports per day don't overwrite the
    // previous file in the user's Downloads folder.
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.download = `meroshare-history-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
    toast(`Exported ${rows.length} row${rows.length === 1 ? '' : 's'}`);
  }

  async function copyLogs() {
    const s = Store.state.logs;
    if (!s.data || !s.data.length) { toast('Nothing to copy', 'info'); return; }
    const lines = logFilter === 'ALL' ? s.data : s.data.filter((ln) => detectLogLevel(ln) === logFilter);
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      toast(`Copied ${lines.length} line${lines.length === 1 ? '' : 's'}`);
    } catch {
      toast('Copy failed. Clipboard permission denied?', 'error');
    }
  }

  function setLogFilter(level) {
    logFilter = level;
    $$('#logToolbar .filter-btn[data-level]').forEach((b) => {
      const on = b.dataset.level === level;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    renderLogs(Store.state.logs);
    updateLogCounts();
  }

  function updateLogCounts() {
    const lines = Store.state.logs.data || [];
    const counts = { ALL: lines.length, ERROR: 0, WARNING: 0, INFO: 0 };
    for (const ln of lines) {
      const lvl = detectLogLevel(ln);
      if (lvl in counts) counts[lvl]++;
    }
    $$('#logToolbar .filter-btn[data-level]').forEach((b) => {
      const level = b.dataset.level;
      const baseLabel = { ALL: 'All', ERROR: 'Error', WARNING: 'Warning', INFO: 'Info' }[level] || level;
      b.textContent = `${baseLabel} (${counts[level] ?? 0})`;
    });
  }

  function renderDashStatus() {
    const sched = Store.state.scheduler.data || {};
    const accts = Store.state.accounts.data || [];
    const issues = Store.state.issues.data || [];
    const bits = [];
    if (sched.enabled) {
      bits.push(`Scheduler: active (every ${sched.interval_hours || '?'}h)`);
      if (sched.next_run) {
        const next = durationToNext(sched.next_run);
        bits.push(`next ${next}`);
      } else if (sched.last_run) {
        bits.push(`last ${relTime(sched.last_run)}`);
      }
    } else {
      bits.push('Scheduler: inactive');
    }
    bits.push(`${accts.length} account${accts.length === 1 ? '' : 's'}`);
    if (issues.length) bits.push(`${issues.length} open issue${issues.length === 1 ? '' : 's'}`);
    const node = $('#dashStatusText');
    if (node) node.textContent = bits.join(' · ');
  }

  // ── Apply flow ─────────────────────────────────────────────────
  let applyPolling = false;

  // Format a number as Nepali rupee with thousand-separators, no decimals.
  // Rs. 1500000 → "Rs. 15,00,000" — Indian/Nepali grouping (lakh-style)
  // is what the user reads on MeroShare itself, so matching it avoids
  // a "wait, that doesn't match" cognitive jolt at apply time.
  function formatNpr(n) {
    if (!isFinite(n) || n <= 0) return '';
    const rounded = Math.round(n);
    const s = String(rounded);
    if (s.length <= 3) return `Rs. ${s}`;
    const last3 = s.slice(-3);
    let rest = s.slice(0, -3);
    rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',');
    return `Rs. ${rest},${last3}`;
  }

  // Build a "≈ Rs. X across N account(s)" preview line for the apply
  // modal so the user has a concrete cost in front of them at confirm
  // time. Returns '' when we can't compute confidently — better to
  // omit than mislead with a fake number.
  function previewApplyCost(issueId, accountIds, category, globalKitta, maxAmount) {
    const issue = (Store.state.issues.data || []).find((i) => String(i.id) === String(issueId));
    if (!issue) return '';
    const price = Number(issue.sharePerUnit);
    if (!isFinite(price) || price <= 0) {
      // No share price means we can't compute. Still useful to show
      // *which* accounts will be hit, even without a number.
      return `${accountIds.length} account(s) selected.`;
    }
    if (category === 'right_share') {
      // Right shares apply max-eligible per account, which we don't
      // know without browser-fetching the form. Show the cap instead
      // so the user sees the upper bound that matters financially.
      if (maxAmount) {
        const cap = formatNpr(maxAmount * accountIds.length);
        return `Right share: capped at ${formatNpr(maxAmount)} per account (≤ ${cap} total at ${formatNpr(price)}/share).`;
      }
      return `Right share: applies maximum eligible per account (no cap configured — set max_amount in Settings).`;
    }
    // IPO / FPO / others: use per-account override when present.
    const accountsById = Object.fromEntries(
      (Store.state.accounts.data || []).map((a) => [a.id, a]),
    );
    let totalKitta = 0;
    const breakdown = [];
    for (const aid of accountIds) {
      const acct = accountsById[aid];
      const k = (acct && acct.default_kitta) || globalKitta;
      totalKitta += k;
      breakdown.push(`${acct ? acct.name : aid}: ${k} kitta`);
    }
    let total = totalKitta * price;
    let cappedNote = '';
    if (maxAmount && (totalKitta * price) / accountIds.length > maxAmount) {
      // Per-account cap. Recompute the practical max.
      const cappedKittaPerAccount = Math.max(1, Math.floor(maxAmount / price));
      total = cappedKittaPerAccount * price * accountIds.length;
      cappedNote = ` (capped at ${formatNpr(maxAmount)}/account)`;
    }
    return `≈ ${formatNpr(total)} across ${accountIds.length} account(s) at ${formatNpr(price)}/share${cappedNote}.`;
  }

  async function applyForIssue(id, name, accountIds, category, btn) {
    const label = (accountIds && accountIds.length > 1) ? `${accountIds.length} accounts` : 'this account';
    const cfg = Store.state.config.data || {};
    const aa = cfg.auto_apply || {};
    const globalKitta = aa.default_kitta || 10;
    const maxAmount = aa.max_amount && aa.max_amount > 0 ? aa.max_amount : null;
    const detail = category === 'right_share' && aa.right_share_apply_max !== false
      ? `Right share: applies maximum eligible kitta (the form's max).`
      : `Default kitta per account: ${globalKitta} (per-account overrides apply where set).`;
    const costLine = previewApplyCost(id, accountIds, category, globalKitta, maxAmount);
    const ok = await modal({
      title: `Apply for ${name}?`,
      body: `This will submit applications on ${label}. ${detail}${costLine ? '\n\n' + costLine : ''}\n\nYou can only undo on MeroShare directly.`,
      confirmLabel: 'Apply',
      confirmVariant: 'btn-primary',
    });
    if (!ok) return;
    // Flip the row's action button to an "Applying…" state immediately.
    // Without this, the only feedback is a transient toast and the
    // button stays clickable — confusing if the toast is missed or
    // dismissed quickly. The next /api/issues refresh (triggered by
    // pollApplyStatus' onDone) will replace the button entirely; if
    // the POST fails up front we restore the original label below.
    let originalLabel = '';
    if (btn) {
      originalLabel = btn.textContent;
      btn.disabled = true;
      btn.classList.add('is-loading');
      btn.textContent = 'Applying…';
    }
    const restoreBtn = () => {
      if (!btn) return;
      btn.disabled = false;
      btn.classList.remove('is-loading');
      btn.textContent = originalLabel;
    };
    try {
      const res = await fetch(`/api/apply/${id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_ids: accountIds || [], category: category || '' }),
      });
      const body = await res.json();
      if (body.error) {
        toast(body.error, 'error');
        restoreBtn();
        return;
      }
      toast(`Applying for ${name}…`, 'info');
      pollApplyStatus();
    } catch {
      toast('Failed to start apply', 'error');
      restoreBtn();
    }
  }

  // Shared poll loop for /api/apply-status. Used by both the
  // dashboard's apply flow and the "Run check now" button. Hard
  // timeout ensures a wedged Playwright worker can't leave the UI
  // disabled forever; AbortController prevents a late response from
  // re-toggling state after timeout.
  function _pollApplyJobStatus({
    timeoutMs = 10 * 60 * 1000,
    intervalMs = 1500,
    doneToast = 'Done',
    timeoutToast = 'Still running after 10 min. Check logs.',
    onStop = () => {},
    onDone = () => {},
  } = {}) {
    const startedAt = Date.now();
    const state = { timer: null, abort: null, alive: true };

    const stop = () => {
      if (!state.alive) return;
      state.alive = false;
      if (state.timer) { clearTimeout(state.timer); state.timer = null; }
      if (state.abort) {
        try { state.abort.abort(); } catch {}
        state.abort = null;
      }
      onStop();
    };

    const tick = () => {
      state.timer = null;
      if (Date.now() - startedAt > timeoutMs) {
        stop();
        toast(timeoutToast, 'error');
        return;
      }
      state.abort = new AbortController();
      fetch('/api/apply-status', { signal: state.abort.signal })
        .then((r) => r.json()).then((d) => {
          if (d.running) {
            state.timer = setTimeout(tick, intervalMs);
            return;
          }
          stop();
          toast(d.message || doneToast);
          if (d.results) {
            for (const [, r] of Object.entries(d.results)) {
              if (r && r.success === false) {
                toast(`${r.accountName}: ${r.message || 'failed'}`, 'error');
              }
            }
          }
          // The applied state on MeroShare may have just changed -
          // force-refresh /api/issues so chips and pending counts
          // reflect the post-apply truth.
          Store.invalidate('issues');
          Store.fetchOnce('issues', { force: true });
          onDone(d);
        }).catch((e) => {
          if (e && e.name === 'AbortError') return;
          stop();
          toast('Lost contact while polling. Refresh the page.', 'error');
        });
    };
    state.timer = setTimeout(tick, intervalMs);
    return stop;
  }

  function pollApplyStatus() {
    if (applyPolling) return;
    applyPolling = true;
    _pollApplyJobStatus({
      onStop: () => { applyPolling = false; },
    });
  }

  // ── Account CRUD ───────────────────────────────────────────────
  function showAccountForm(existing) {
    $('#accountFormCard').style.display = 'block';
    $('#accountFormTitle').textContent = existing ? 'Edit account' : 'Add account';
    $('#acct-id').value = existing ? existing.id : '';
    $('#acct-name').value = existing ? existing.name : '';
    $('#acct-dp').value = existing ? existing.dp_id : '';
    $('#acct-user').value = existing ? existing.username : '';
    // Never prefill password/pin: the GET response masks them with a
    // fixed-width placeholder, and writing that placeholder back to
    // the input would let the user save it accidentally. Leave blank
    // and tell the user via placeholder text that blank means
    // "unchanged".
    $('#acct-pass').value = '';
    $('#acct-pass').placeholder = existing
      ? '(leave blank to keep current)'
      : 'Password';
    $('#acct-crn').value = existing ? existing.crn : '';
    $('#acct-pin').value = '';
    $('#acct-pin').placeholder = existing
      ? '(leave blank to keep current)'
      : '4-digit PIN';
    $('#acct-bank').value = existing && existing.preferred_bank ? existing.preferred_bank : '';
    const bankAcctEl = $('#acct-bank-account');
    if (bankAcctEl) {
      bankAcctEl.value = existing && existing.preferred_bank_account
        ? existing.preferred_bank_account : '';
    }
    const kittaEl = $('#acct-kitta');
    if (kittaEl) {
      // Distinguish 0 (an invalid stored value) from null/undefined
      // ("use global"). Show empty string in both cases so the
      // placeholder hint stays visible.
      kittaEl.value = (existing && existing.default_kitta) || '';
    }
    $('#acct-name').focus();
  }

  function hideAccountForm() {
    $('#accountFormCard').style.display = 'none';
  }

  function clearFormErrors() {
    ['name', 'dp', 'user', 'pass', 'crn', 'pin'].forEach((k) => {
      const e = $('#err-acct-' + k);
      if (e) e.textContent = '';
    });
  }

  function setFieldError(key, message) {
    const e = $('#err-acct-' + key);
    if (e) e.textContent = message;
  }

  function validateAccountForm(data, isEdit) {
    clearFormErrors();
    let ok = true;
    if (!data.name.trim()) { setFieldError('name', 'Account name is required'); ok = false; }
    if (!/^\d+$/.test(data.dp_id)) { setFieldError('dp', 'DP ID must be numeric'); ok = false; }
    if (!data.username.trim()) { setFieldError('user', 'BOID is required'); ok = false; }
    // For edits, password/pin are typed only when the user wants to
    // change them. Leaving them blank means "keep the existing value".
    // Only enforce on add.
    if (!isEdit && !data.password) { setFieldError('pass', 'Password is required'); ok = false; }
    if (!data.crn.trim()) { setFieldError('crn', 'CRN is required'); ok = false; }
    // PIN: 4 digits required on add. On edit, blank => unchanged; any
    // typed value must be exactly 4 digits (no more masked-shape
    // tolerance. The server uses a fixed-width placeholder, the GUI
    // keeps the pin field blank on edit).
    if (!isEdit && !/^\d{4}$/.test(data.pin)) {
      setFieldError('pin', 'PIN must be exactly 4 digits');
      ok = false;
    } else if (isEdit && data.pin && !/^\d{4}$/.test(data.pin)) {
      setFieldError('pin', 'PIN must be exactly 4 digits (or leave blank to keep current)');
      ok = false;
    }
    return ok;
  }

  async function saveAccount() {
    const id = $('#acct-id').value;
    const bankAcctEl = $('#acct-bank-account');
    const kittaEl = $('#acct-kitta');
    const data = {
      name: $('#acct-name').value,
      dp_id: $('#acct-dp').value,
      username: $('#acct-user').value,
      password: $('#acct-pass').value,
      crn: $('#acct-crn').value,
      pin: $('#acct-pin').value,
      preferred_bank: $('#acct-bank').value,
      preferred_bank_account: bankAcctEl ? bankAcctEl.value : '',
      // Empty string is the explicit "clear" sentinel the server
      // honors. Leaving the field absent from the payload would mean
      // "don't change" on update, but on add it lets a typo slip
      // through without surfacing a server-side validation error.
      default_kitta: kittaEl ? kittaEl.value : '',
    };
    if (!validateAccountForm(data, !!id)) {
      toast('Fix the highlighted fields', 'error');
      return;
    }
    // On edit: drop blank password/pin so the server treats them as
    // "unchanged" rather than receiving an empty string and rejecting
    // it. Server's update() also skips falsy values, but stripping at
    // the source is clearer and avoids the "what if the validator
    // changes" footgun.
    if (id) {
      if (!data.password) delete data.password;
      if (!data.pin) delete data.pin;
    }
    const url = id ? `/api/accounts/${encodeURIComponent(id)}` : '/api/accounts';
    const method = id ? 'PUT' : 'POST';
    try {
      const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
      // Tolerate non-JSON bodies (e.g. 500 with HTML stack page) so we
      // don't fall through to the generic catch and hide the real
      // error from the user.
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        const msg = (body && body.error) || `Server returned HTTP ${res.status}`;
        toast(msg, 'error');
        return;
      }
      if (body && body.error) { toast(body.error, 'error'); return; }
      toast('Account saved');
      hideAccountForm();
      Store.invalidate('accounts');
      Store.fetchOnce('accounts');
    } catch {
      toast('Could not save account', 'error');
    }
  }

  async function deleteAccount(id, name) {
    const ok = await modal({
      title: `Delete "${name}"?`,
      body: 'The account is removed from this app. Application history on MeroShare itself is unaffected.',
      confirmLabel: 'Delete',
      confirmVariant: 'btn-danger-outline',
    });
    if (!ok) return;
    try {
      const res = await fetch(`/api/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' });
      const body = await res.json();
      if (body.error) { toast(body.error, 'error'); return; }
      toast('Account deleted');
      Store.invalidate('accounts');
      Store.fetchOnce('accounts');
    } catch {
      toast('Could not delete account', 'error');
    }
  }

  async function testAccountLogin(id, name) {
    toast(`Testing ${name}…`, 'info');
    try {
      const res = await fetch(`/api/accounts/${encodeURIComponent(id)}/test-login`, { method: 'POST' });
      const d = await res.json();
      lastTestStatus[id] = {
        ok: !!d.success,
        name: d.name || null,
        demat: d.demat || null,
        boid: d.boid || null,
        // Capture MeroShare's expiry dates so we can warn the user
        // before their CRN/PIN access actually breaks during an IPO
        // open window (the worst possible time to discover this).
        expiredDate: d.expiredDate || null,
        passwordExpiryDate: d.passwordExpiryDate || null,
        ts: Date.now(),
      };
      _persistTestStatus();
      if (d.success) toast(`Login OK for ${name} (${d.name})`);
      else toast(`Login failed for ${name}: ${d.error}`, 'error');
      renderAccounts(Store.state.accounts);
      renderExpiryBanner();
    } catch {
      lastTestStatus[id] = {
        ok: false, name: null, demat: null, boid: null,
        expiredDate: null, passwordExpiryDate: null, ts: Date.now(),
      };
      _persistTestStatus();
      toast(`Could not test ${name}`, 'error');
      renderAccounts(Store.state.accounts);
      renderExpiryBanner();
    }
  }

  async function saveConfig({ silent = false } = {}) {
    const cfg = JSON.parse(JSON.stringify(Store.state.config.data || {}));
    cfg.share_types = {
      ipo_ordinary: $('#pref-ipo').checked,
      right_share:  $('#pref-right').checked,
      fpo:          $('#pref-fpo').checked,
      mutual_fund:  $('#pref-mf').checked,
      debenture:    $('#pref-deb').checked,
    };
    cfg.auto_apply = Object.assign(cfg.auto_apply || {}, {
      enabled: true,
      default_kitta: parseInt($('#pref-kitta').value, 10) || 10,
      right_share_apply_max: $('#pref-rightmax').checked,
      max_amount: parseInt($('#pref-maxamt').value, 10) || 100000,
    });
    try {
      const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
      const body = await res.json();
      if (body.error) { toast(body.error, 'error'); return; }
      // Silent saves (auto-save on every toggle) skip the toast
      // because firing one for every flip would be obnoxious. The
      // "Saved at HH:MM:SS" stamp under the Save button is still
      // updated so the user has a visible cue.
      if (!silent) toast('Settings saved');
      Store.set('config', { data: cfg, fetchedAt: Date.now() });
      const stamp = $('#savedStamp');
      if (stamp) {
        const t = new Date();
        stamp.textContent = `Saved at ${t.toLocaleTimeString()}`;
      }
    } catch {
      toast('Could not save settings', 'error');
    }
  }

  // Auto-save: debounce so a slider drag or rapid toggle batch into one
  // POST. 300ms is comfortable — fast enough to feel "live", long
  // enough to coalesce real bursts.
  let _autoSaveTimer = null;
  function scheduleAutoSave() {
    if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
    _autoSaveTimer = setTimeout(() => {
      _autoSaveTimer = null;
      saveConfig({ silent: true });
    }, 300);
  }

  // ── Scheduler ──────────────────────────────────────────────────
  async function setSchedulerEnabled(enabled) {
    const interval = parseInt($('#sched-interval').value, 10);
    const url = enabled ? '/api/scheduler/start' : '/api/scheduler/stop';
    const body = enabled ? JSON.stringify({ interval_hours: interval }) : null;
    try {
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
      const data = await res.json();
      if (data.error) { toast(`Scheduler error: ${data.error}`, 'error'); Store.fetchOnce('scheduler', { force: true }); return; }
      Store.set('scheduler', { data, fetchedAt: Date.now() });
      toast(enabled ? 'Scheduler started' : 'Scheduler stopped');
    } catch {
      toast('Could not reach the app', 'error');
      Store.fetchOnce('scheduler', { force: true });
    }
  }

  // ── Run-now ────────────────────────────────────────────────────
  async function runCheckNow() {
    const btn = $('#runCheckBtn');
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = 'Running…';
    try {
      const res = await fetch('/api/run-check', { method: 'POST' });
      const body = await res.json();
      if (body.error) {
        toast(body.error, 'error');
        btn.disabled = false; btn.textContent = 'Run check now';
        return;
      }
      toast('Check started', 'info');
      _pollApplyJobStatus({
        doneToast: 'Check complete',
        timeoutToast: 'Check still running after 10 min. See logs.',
        onStop: () => {
          btn.disabled = false; btn.textContent = 'Run check now';
        },
        onDone: () => {
          // Run-check may have changed the launchd "last run"
          // timestamp; refresh the scheduler card too.
          Store.invalidate('scheduler');
          Store.fetchOnce('scheduler');
        },
      });
    } catch {
      btn.disabled = false; btn.textContent = 'Run check now';
      toast('Could not start check', 'error');
    }
  }

  // ── Stop / close ───────────────────────────────────────────────
  function fullScreenMessage(heading, body) {
    const wrap = el('div', { style: 'display:flex;align-items:center;justify-content:center;min-height:100vh;flex-direction:column;color:#94a3b8;font-family:-apple-system,system-ui,sans-serif;text-align:center;padding:20px;background:#0a0e1a;' }, [
      el('h2', { style: 'font-size:18px;margin-bottom:8px;color:#e8eef7;', text: heading }),
      el('p', { style: 'font-size:14px;max-width:420px;line-height:1.55;', text: body }),
    ]);
    document.body.replaceChildren(wrap);
  }

  function showStoppedScreen() {
    fullScreenMessage(
      'Stopped',
      'The GUI and background scheduler are both off. To start again, run ./run.sh.',
    );
  }

  function showSoftCloseScreen() {
    fullScreenMessage(
      'You can close this tab',
      'The web app and background scheduler keep running. Open http://localhost:5050 anytime to come back.',
    );
  }

  async function quitApp() {
    const ok = await modal({
      title: 'Stop everything?',
      body: 'Shuts down both the GUI and the background scheduler. Run ./run.sh again to start fresh.',
      confirmLabel: 'Stop everything',
      confirmVariant: 'btn-danger-outline',
    });
    if (!ok) return;
    try {
      const res = await fetch('/api/shutdown', { method: 'POST' });
      if (res.status === 409) {
        const body = await res.json();
        toast(body.error || 'Cannot stop right now', 'error');
        return;
      }
      // The server may have failed to unload the scheduler agent
      // (launchctl perms, missing plist). Surface that so the user
      // doesn't think "stop everything" silently succeeded while the
      // scheduler keeps firing. Use a 30s sticky toast so the warning
      // remains visible even on the post-stop screen.
      const body = await res.json().catch(() => null);
      if (body && body.scheduler_warning) {
        toast(
          'Scheduler may still be running. ' + body.scheduler_warning,
          'error',
        );
      }
      showStoppedScreen();
    } catch {
      showStoppedScreen();
    }
  }

  function closeApp() {
    // window.close() only works on tabs JavaScript opened (browsers
    // refuse on user-opened tabs). Show the soft overlay first as a
    // hint, then attempt to close in case the browser allows it.
    showSoftCloseScreen();
    setTimeout(() => { try { window.close(); } catch (e) { /* ignored */ } }, 150);
  }

  // ── Browser tab title ──────────────────────────────────────────
  function updateTabTitle(s) {
    if (!s.data) return;
    let pending = 0;
    for (const issue of s.data) {
      const apps = issue.applications || {};
      for (const a of Object.values(apps)) if (!a.applied) pending++;
    }
    document.title = pending > 0
      ? `(${pending}) MeroShare Auto-Apply`
      : 'MeroShare Auto-Apply';
  }

  // ── Tabs ───────────────────────────────────────────────────────
  // Tab switching is purely visual now. Data is already in the Store
  // from the initial sync (or being fetched). No per-tab fetches.
  function showPage(name, btn) {
    activeTab = name;
    $$('.page').forEach((p) => p.classList.remove('active'));
    $$('.nav button').forEach((b) => {
      b.classList.remove('active');
      b.setAttribute('aria-selected', 'false');
    });
    $('#page-' + name).classList.add('active');
    if (btn) {
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
    }
    // Persist active tab to the URL hash so a hot-reload (or manual
    // browser refresh) returns to the same tab.
    if (location.hash !== '#' + name) {
      history.replaceState(null, '', '#' + name);
    }
    // Lazy sources are fetched on first activation of their tab. After
    // that, the background tick keeps them fresh per-TTL.
    if (name === 'history') {
      Store.fetchOnce('history');
      Store.fetchOnce('applied');
    }
    // The dashboard's lifetime-stats card also needs the /api/status
    // history payload. Trigger the same lazy fetch — the 5-min TTL
    // means subsequent dashboard renders are free, and we still
    // avoid hitting MeroShare on EVERY page load (the lazy + TTL
    // combination piggybacks history onto the user's first dashboard
    // visit and keeps it warm from there).
    if (name === 'dashboard') {
      Store.fetchOnce('history');
    }
  }

  // ── Unified sync ───────────────────────────────────────────────
  // Fetches everything in parallel. fetchOnce respects the per-source
  // TTL, so a non-forcing call from the background tick is cheap when
  // sources are still fresh.
  function syncAll({ force = false } = {}) {
    // Skip lazy sources (currently History) on the AUTOMATIC sync to
    // avoid hammering MeroShare with simultaneous per-account logins.
    // Manual refresh re-invalidates them in manualRefresh().
    //
    // Order matters on first paint: the issues fetch does N MeroShare
    // logins, which can take 10-30s under WAF mood swings. Don't make
    // accounts/config/scheduler/logs wait on it. Start them as a
    // batch first, then kick off issues separately so the UI's quick
    // states (account list, schedule status) paint immediately.
    const NON_LAZY = Object.keys(Store.state).filter((k) => !Store.state[k].lazy);
    const SLOW = new Set(['issues']);
    const fast = NON_LAZY.filter((k) => !SLOW.has(k));
    const slow = NON_LAZY.filter((k) => SLOW.has(k));
    if (force) {
      NON_LAZY.forEach((k) => Store.invalidate(k));
    }
    const fastP = Promise.all(fast.map((key) =>
      Store.fetchOnce(key, { force: force })
    ));
    // Fire slow fetches without awaiting. They update the UI when
    // they land. The returned promise still resolves on the fast
    // batch so callers (manualRefresh, init) don't block on issues.
    slow.forEach((key) => {
      Store.fetchOnce(key, { force: force && key === 'issues' });
    });
    return fastP;
  }

  function manualRefresh() {
    syncAll({ force: true });
    // Include lazy sources only when the user has actually loaded
    // them before (i.e. they're "alive" in the cache); otherwise the
    // manual refresh would trigger heavy MeroShare logins for tabs
    // the user never opened.
    Object.keys(Store.state).forEach((key) => {
      const s = Store.state[key];
      if (!s.lazy) return;
      if (s.fetchedAt) {
        Store.invalidate(key);
        Store.fetchOnce(key);
      }
    });
  }

  // ── Header quick-actions ───────────────────────────────────────
  // When `next_run` falls in the past we want to refetch the scheduler
  // status, but the previous code did that on every call. And
  // renderHeaderQuickActions runs every second, so we'd hammer
  // /api/scheduler at 1Hz indefinitely. Throttle the refetch to once
  // per minute by remembering when we last asked.
  let _schedulerOverdueRefetchAt = 0;

  // Side-effect kept separate from the formatter. DurationToNext used
  // to fire a network fetch from inside what looked like a pure
  // string formatter, which surprised future readers and made the
  // function harder to reason about.
  function _maybeRefetchOverdueScheduler() {
    const now = Date.now();
    if (now - _schedulerOverdueRefetchAt > 60_000) {
      _schedulerOverdueRefetchAt = now;
      Store.invalidate('scheduler');
      Store.fetchOnce('scheduler');
    }
  }

  function durationToNext(nextRunIso) {
    const t = new Date(String(nextRunIso).replace(' ', 'T')).getTime();
    if (Number.isNaN(t)) return null;
    const ms = t - Date.now();
    if (ms <= 0) {
      _maybeRefetchOverdueScheduler();
      // Show how overdue the run is so a stalled scheduler is
      // visible. Past ~5 min the user should be suspicious that
      // launchd hasn't fired.
      const overdueMin = Math.floor(-ms / 60000);
      if (overdueMin <= 0) return 'due';
      if (overdueMin >= 60) return `overdue ${Math.floor(overdueMin / 60)}h ${overdueMin % 60}m`;
      return `overdue ${overdueMin}m`;
    }
    const totalMins = Math.round(ms / 60000);
    const hrs = Math.floor(totalMins / 60);
    const mins = totalMins % 60;
    if (hrs > 0) return `${hrs}h ${mins}m`;
    return `${mins}m`;
  }

  function renderHeaderQuickActions() {
    const sched = Store.state.scheduler.data || {};
    const btn = $('#schedQuickBtn');
    if (!btn) return;

    btn.classList.toggle('active', !!sched.enabled);

    // Strip any prior label so we don't accumulate spans on each tick.
    btn.querySelectorAll('.sched-label').forEach((n) => n.remove());

    if (sched.enabled) {
      const label = sched.next_run ? durationToNext(sched.next_run)
                  : sched.interval_hours ? `${sched.interval_hours}h`
                  : 'on';
      if (label) {
        btn.classList.add('has-label');
        btn.appendChild(el('span', { class: 'sched-label', text: label }));
      } else {
        btn.classList.remove('has-label');
      }
      const tip = sched.next_run
        ? `Background scheduler: next run in ${label}. Click to stop`
        : `Background scheduler: every ${sched.interval_hours || '?'}h. Click to stop`;
      btn.setAttribute('title', tip);
    } else {
      btn.classList.remove('has-label');
      btn.setAttribute('title', 'Background scheduler: inactive. Click to start');
    }
  }

  async function toggleSchedulerFromHeader() {
    const sched = Store.state.scheduler.data || {};
    const willEnable = !sched.enabled;
    if (willEnable) {
      await setSchedulerEnabled(true);
    } else {
      const ok = await modal({
        title: 'Stop background scheduler?',
        body: 'New issues will no longer be checked automatically until you re-enable it.',
        confirmLabel: 'Stop',
        confirmVariant: 'btn-danger-outline',
      });
      if (ok) await setSchedulerEnabled(false);
    }
  }

  // ── Wiring ─────────────────────────────────────────────────────
  function wireUp() {
    // Subscribers. Render reacts to Store changes.
    Store.subscribe('issues', (s) => { renderIssues(s); renderDashStatus(); });
    Store.subscribe('history', (s) => { renderHistory(s); renderStatsCard(s); });
    Store.subscribe('applied', renderAppliedCache);
    Store.subscribe('accounts', (s) => {
      renderAccounts(s); renderDashStatus(); renderHistoryToolbar();
      renderExpiryBanner();
    });
    Store.subscribe('config', renderConfig);
    Store.subscribe('scheduler', (s) => {
      renderSchedulerStatus(s);
      renderDashStatus();
      renderHeaderQuickActions();
      renderSchedulerHealth();
    });
    Store.subscribe('logs', (s) => { renderLogs(s); updateLogCounts(); });

    // Re-render the sync indicator on any state change.
    for (const key of Object.keys(Store.state)) {
      Store.subscribe(key, () => renderSyncMeta());
    }

    // Two intervals. The cheap 1s tick keeps live counters fresh
    // (sync age, scheduler countdown); the heavier 5s tick decides
    // whether to actually re-fetch any stale sources.
    setInterval(() => {
      renderSyncMeta();
      renderHeaderQuickActions();
    }, 1000);
    setInterval(() => {
      Object.keys(Store.state).forEach((key) => {
        const s = Store.state[key];
        // Don't auto-refresh lazy sources unless they've already been
        // loaded once (TTL-based refresh is fine; first-time fetch
        // waits for explicit user activation).
        if (s.lazy && !s.fetchedAt) return;
        if (!Store.isFresh(key)) Store.fetchOnce(key);
      });
    }, 5000);

    // Nav buttons (visual only. Data already loading from initial sync).
    $$('.nav button').forEach((b) => {
      b.addEventListener('click', () => showPage(b.dataset.tab, b));
    });

    // Header.
    $('#refreshBtn').addEventListener('click', manualRefresh);
    $('#schedQuickBtn').addEventListener('click', toggleSchedulerFromHeader);
    $('#closeQuickBtn').addEventListener('click', closeApp);
    $('#quitQuickBtn').addEventListener('click', quitApp);
    $('#quitQuickBtn').classList.add('danger');

    // Logs filter (level buttons only. Copy is wired separately).
    $$('#logToolbar .filter-btn[data-level]').forEach((b) => {
      b.addEventListener('click', () => setLogFilter(b.dataset.level));
    });
    $('#copyLogsBtn').addEventListener('click', copyLogs);

    // History search + CSV export.
    $('#historySearch').addEventListener('input', (e) => {
      historyFilters.q = e.target.value.trim();
      renderHistory(Store.state.history);
    });
    $('#exportCsvBtn').addEventListener('click', exportHistoryCsv);

    // Update browser tab title with pending-application count so the
    // user sees activity at a glance from any tab.
    Store.subscribe('issues', updateTabTitle);

    // Dashboard.
    $('#runCheckBtn').addEventListener('click', runCheckNow);

    // Settings. Toggles and inputs.
    $('#sched-toggle').addEventListener('change', (e) => setSchedulerEnabled(e.target.checked));
    $('#sched-interval').addEventListener('change', () => {
      if ($('#sched-toggle').checked) setSchedulerEnabled(true);
    });
    $('#saveConfigBtn').addEventListener('click', () => saveConfig());
    // Auto-save: every config-bound toggle and input persists on
    // change. Without this, users were flipping FPO / max-amount / etc.
    // and not realizing the "Save settings" button (in a different
    // section) was needed — a refresh would silently revert the
    // toggle. macOS Settings.app expectation is "flip = saved".
    const autoSaveIds = [
      // Share preferences
      'pref-ipo', 'pref-right', 'pref-fpo', 'pref-mf', 'pref-deb',
      // Application defaults
      'pref-kitta', 'pref-maxamt', 'pref-rightmax',
    ];
    for (const id of autoSaveIds) {
      const el = document.getElementById(id);
      if (el) {
        // For checkboxes 'change' is the right event; for number
        // inputs we also bind 'input' so typing flushes through the
        // debounce, not waiting for blur.
        el.addEventListener('change', scheduleAutoSave);
        if (el.type === 'number') {
          el.addEventListener('input', scheduleAutoSave);
        }
      }
    }
    $('#resetConfigBtn').addEventListener('click', async () => {
      const ok = await modal({
        title: 'Reset settings to defaults?',
        body: 'Resets share-type toggles, default kitta, max amount, and the right-share-max flag. Accounts and the scheduler are unaffected.',
        confirmLabel: 'Reset',
        confirmVariant: 'btn-danger-outline',
      });
      if (!ok) return;
      const defaults = {
        share_types: { ipo_ordinary: true, right_share: true, fpo: false, mutual_fund: false, debenture: false },
        auto_apply: { enabled: true, default_kitta: 10, right_share_apply_max: true, max_amount: 100000 },
      };
      // Splice into the in-memory config, save, re-render.
      const current = Store.state.config.data || {};
      const merged = Object.assign({}, current, defaults);
      Store.set('config', { data: merged, fetchedAt: Date.now() });
      await saveConfig();
    });
    $('#addAccountBtn').addEventListener('click', () => showAccountForm());
    // Backup + restore.
    $('#backupBtn').addEventListener('click', () => {
      // Plain navigation. Flask sends Content-Disposition for download.
      window.location.href = '/api/backup';
    });
    $('#restoreBtn').addEventListener('click', () => $('#restoreFile').click());
    $('#restoreFile').addEventListener('change', async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const ok = await modal({
        title: 'Restore from backup?',
        body: `Replaces all accounts and applied state with the contents of "${file.name}". This is not reversible.`,
        confirmLabel: 'Restore',
        confirmVariant: 'btn-danger-outline',
      });
      if (!ok) { e.target.value = ''; return; }
      try {
        const text = await file.text();
        const json = JSON.parse(text);
        const res = await fetch('/api/restore', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(json),
        });
        const body = await res.json();
        if (body.error) { toast(body.error, 'error'); return; }
        toast(`Restored ${body.accounts} account(s)`);
        // Wipe every cached source so the new state shows everywhere.
        Object.keys(Store.state).forEach((k) => Store.invalidate(k));
        syncAll({ force: true });
      } catch (err) {
        toast('Restore failed: ' + err.message, 'error');
      } finally {
        e.target.value = '';
      }
    });

    $('#testAllBtn').addEventListener('click', async () => {
      const accts = Store.state.accounts.data || [];
      if (!accts.length) { toast('No accounts to test', 'error'); return; }
      const btn = $('#testAllBtn');
      btn.disabled = true;
      btn.textContent = 'Testing…';
      // Sequential, not parallel. Multiple simultaneous logins from one
      // IP can trigger MeroShare's rate limiting.
      for (const a of accts) {
        await testAccountLogin(a.id, a.name);
      }
      btn.disabled = false;
      btn.textContent = 'Test all logins';
    });
    $('#cancelAccountBtn').addEventListener('click', hideAccountForm);
    $('#saveAccountBtn').addEventListener('click', saveAccount);
    $$('.reveal-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const target = document.getElementById(btn.dataset.target);
        if (!target) return;
        const reveal = target.type === 'password';
        target.type = reveal ? 'text' : 'password';
        btn.textContent = reveal ? 'hide' : 'show';
      });
    });
    $('#quitBtn').addEventListener('click', quitApp);

    // Footer help link.
    $('#helpLink').addEventListener('click', (e) => {
      e.preventDefault();
      modal({
        title: 'About MeroShare Auto-Apply',
        body:
          'A retail-investor IPO/right-share auto-applier for NEPSE.\n\n' +
          'Header icons (left to right):\n' +
          '· Refresh. Reloads issues, accounts, scheduler, logs in one go.\n' +
          '· Schedule. Opens the background scheduler interval picker.\n' +
          '· Close. Closes this browser tab.\n' +
          '· Power. Stops both this GUI and the launchd scheduler.\n\n' +
          'Keyboard:\n' +
          'R refresh · 1-4 switch tabs · Esc/Enter close modals\n\n' +
          'github.com/OfficialBishal/MeroShare-Auto-Apply',
        confirmLabel: 'Close',
        confirmVariant: 'btn-primary',
      });
    });

    // Keyboard shortcuts. Skip if the user is typing in a form, so 'r'
    // doesn't get hijacked while they're entering a password.
    document.addEventListener('keydown', (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (document.activeElement?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
      if (e.key === 'r' || e.key === 'R') { e.preventDefault(); manualRefresh(); }
      if (e.key === '1') showPage('dashboard', $('.nav button[data-tab="dashboard"]'));
      if (e.key === '2') showPage('history', $('.nav button[data-tab="history"]'));
      if (e.key === '3') showPage('settings', $('.nav button[data-tab="settings"]'));
      if (e.key === '4') showPage('logs', $('.nav button[data-tab="logs"]'));
      if (e.key === '?') {
        e.preventDefault();
        modal({
          title: 'Keyboard shortcuts',
          body: 'R. Refresh all data\n1. Dashboard\n2. History\n3. Settings\n4. Logs\n?. Show this help\nEsc. Close modal\nEnter. Confirm modal',
          confirmLabel: 'Got it',
          confirmVariant: 'btn-primary',
        });
      }
    });

    // First load. Pick the tab from URL hash (so hot-reload returns to
    // the same tab), the path (/settings → settings), or default to
    // dashboard. The hash takes precedence and may carry a query
    // string like `#dashboard?issue=123` (the menu bar uses this to
    // deep-link into a specific issue).
    const HASH_TABS = ['dashboard', 'history', 'settings', 'logs'];
    const rawHash = location.hash.replace(/^#/, '');
    const [hashTab, hashQuery] = rawHash.split('?', 2);
    const initialTab = HASH_TABS.includes(hashTab)
      ? hashTab
      : (location.pathname.indexOf('settings') !== -1 ? 'settings' : 'dashboard');
    const initialBtn = document.querySelector(`.nav button[data-tab="${initialTab}"]`);
    showPage(initialTab, initialBtn);
    // Surface the deep-link target after first paint. Looks for
    // `?issue=<id>` and scrolls/highlights the matching row when
    // the issues list lands.
    if (hashQuery) {
      const params = new URLSearchParams(hashQuery);
      const wantIssue = params.get('issue');
      if (wantIssue) {
        const tryFocus = () => {
          const row = document.querySelector(`[data-issue-id="${wantIssue}"]`);
          if (row) {
            row.scrollIntoView({ behavior: 'smooth', block: 'center' });
            row.classList.add('issue-flash');
            setTimeout(() => row.classList.remove('issue-flash'), 2000);
            return true;
          }
          return false;
        };
        if (!tryFocus()) {
          // Issues haven't rendered yet; wait for the next Store
          // update on the issues source.
          Store.subscribe('issues', () => { tryFocus(); });
        }
      }
    }
    syncAll();

    // Live reload poller. When the server restarts (file edit picked
    // up by the reloader), boot_ts changes and we silently swap the
    // page for the fresh build. Skip if the user is actively typing
    // in a form so we don't blow away in-progress edits.
    //
    // Exponential backoff on consecutive errors so a server that's
    // taking a while to restart (or a 5xx loop) doesn't get hammered
    // at 2Hz from every open tab.
    const initialBoot = window.__BOOT_TS__ || null;
    let nextDelay = 2000;
    const MAX_DELAY = 30_000;
    const tick = async () => {
      try {
        const res = await fetch('/api/version', { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        nextDelay = 2000;
        if (initialBoot && String(data.boot_ts) !== String(initialBoot)) {
          const inForm = document.activeElement && /^(input|select|textarea)$/i.test(document.activeElement.tagName);
          if (!inForm) {
            location.reload();
            return;
          }
          // User is editing. Recheck again sooner so we still pick
          // up the reload eventually without disrupting them.
          nextDelay = 2000;
        }
      } catch {
        nextDelay = Math.min(MAX_DELAY, Math.round(nextDelay * 1.7));
      }
      setTimeout(tick, nextDelay);
    };
    setTimeout(tick, nextDelay);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireUp);
  } else {
    wireUp();
  }
})();
