/**
 * ReServe Enterprise Admin Portal — Master Zero-Reload SPA Controller (v5.7)
 * - Immediate Pagination Initialization across Users, Food Listings, Donations, Payouts & Audit Logs
 * - Progressive 10-Row "See More (+10)" / "Show Less" inside User Dossier & Directory Modals
 * - Dual-Endpoint 360° Dossier Loader (Fixes Merchant View Modal & Synchronizes Live Dynamic Amounts)
 * - Silent Background Auto-Sync Engine (Updates Tables, KPIs & Notifications without Page Refresh)
 * - Normalized Permit & Accreditation Document Viewer
 */

window.ADMIN_TX_CACHE = {};
window.ADMIN_CHART_INSTANCES = { growth: null, donut: null };
window.ADMIN_PROGRESSIVE_TABLES = {};
window.ADMIN_AUTO_SYNC_TIMER = null;

// =========================================================
// 0. BACK-BUTTON & BFCACHE SECURITY GUARD
// =========================================================
(function enforceAdminAuthGuard() {
  function checkAuth() {
    const path = window.location.pathname || '';
    const isPublicAuthPage =
      path.includes('/login') ||
      path.includes('/reset-password') ||
      path.includes('/resubmit');
    if (isPublicAuthPage) return;

    const token = localStorage.getItem('admin_access_token');
    if (!token) {
      window.location.replace('/login/');
    }
  }

  checkAuth();
  window.addEventListener('pageshow', (event) => {
    if (event.persisted || !localStorage.getItem('admin_access_token')) {
      checkAuth();
    }
  });
})();

document.addEventListener('DOMContentLoaded', () => {
  initSpaNavigation();
  reinitWorkspaceScripts();
  initAdminLogin();
  setupGlobalListeners();
  window.syncAdminNotificationBadge();
  initAdminLiveAutoSync();
});

window.addEventListener('load', () => {
  window.triggerAllTablePaginations();
});

// =========================================================
// 0B. PERMIT & ACCREDITATION URL NORMALIZER + VIEWER
// =========================================================
window.normalizePermitUrl = function(rawPath) {
  if (!rawPath) return '';
  let clean = String(rawPath).trim().replace(/\\/g, '/');
  if (clean.startsWith('/media/http://') || clean.startsWith('/media/https://')) {
    clean = clean.replace(/^\/media\//, '');
  }
  if (
    clean.startsWith('http://') ||
    clean.startsWith('https://') ||
    clean.startsWith('data:')
  ) {
    return clean;
  }
  clean = clean.replace(/^\/+/, '');
  if (clean.startsWith('media/')) {
    return '/' + clean;
  }
  return '/media/' + clean;
};

window.openVerifiedPermitDocument = function(merchantId, rawPath, titleLabel = 'Verified Permit Document') {
  const normalizedUrl = window.normalizePermitUrl(rawPath);
  const popup = window.open('', '_blank');

  if (!popup) {
    if (normalizedUrl) window.location.href = normalizedUrl;
    return;
  }

  const safeTitle = String(titleLabel || 'Verified Permit').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const csrfToken = getCSRFToken() || '';
  const cleanMerchId = String(merchantId || '').replace(/\D/g, '');

  popup.document.write(`
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>${safeTitle} — ReServe KYB Inspector</title>
        <style>
          body {
            margin: 0;
            background: #0E2920;
            color: #F4F8F6;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            flex-direction: column;
            min-height: 100vh;
          }
          header {
            background: #12372C;
            border-bottom: 1px solid rgba(255,255,255,0.12);
            padding: 12px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
          }
          .title { font-size: 14px; font-weight: 700; }
          .sub { font-size: 11px; color: #A8D5BA; margin-top: 2px; }
          main {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
          }
          img {
            max-width: 96%;
            max-height: 86vh;
            border-radius: 10px;
            box-shadow: 0 12px 32px rgba(0,0,0,0.45);
            border: 1px solid rgba(255,255,255,0.15);
            background: #ffffff;
            object-fit: contain;
          }
          .status-msg {
            font-size: 13px;
            color: #D8EBDD;
            text-align: center;
          }
        </style>
      </head>
      <body>
        <header>
          <div>
            <div class="title">${safeTitle}</div>
            <div class="sub" id="docSub">${normalizedUrl || 'Resolving document...'}</div>
          </div>
        </header>
        <main>
          <div id="loadingText" class="status-msg" style="display:none;">Resolving verified permit image from server...</div>
          <img id="permitImg" src="${normalizedUrl}" alt="Verified Permit" />
        </main>
        <script>
          (function() {
            const img = document.getElementById('permitImg');
            const loadingText = document.getElementById('loadingText');
            const sub = document.getElementById('docSub');
            const merchId = "${cleanMerchId}";
            const csrf = "${csrfToken}";
            let triedFallback = false;

            async function fetchBackendPermit() {
              if (triedFallback || !merchId) {
                loadingText.style.display = 'block';
                loadingText.textContent = 'Document file could not be found on local storage.';
                return;
              }
              triedFallback = true;
              img.style.display = 'none';
              loadingText.style.display = 'block';
              try {
                const res = await fetch('/api/accounts/ocr/scan-permit/', {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrf
                  },
                  body: JSON.stringify({ merchant_id: merchId })
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.permit_data_url) {
                  loadingText.style.display = 'none';
                  img.style.display = 'block';
                  img.src = data.permit_data_url;
                  sub.textContent = 'Verified KYB Permit Record (MID-' + merchId + ')';
                  return;
                }
              } catch (e) {}
              loadingText.textContent = 'Document image could not be loaded.';
            }

            if (!img.getAttribute('src')) {
              fetchBackendPermit();
            } else {
              img.onerror = fetchBackendPermit;
            }
          })();
        <\/script>
      </body>
    </html>
  `);
  popup.document.close();
};

// =========================================================
// 0C. PROGRESSIVE 10-ROW "SEE MORE (+10)" / "SHOW LESS" ENGINE
// =========================================================
window.renderProgressiveRows = function(tbodyId, rowsHtmlArray, emptyMessageHtml, colSpan = 7, stepSize = 10) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;

  if (!Array.isArray(rowsHtmlArray) || rowsHtmlArray.length === 0) {
    delete window.ADMIN_PROGRESSIVE_TABLES[tbodyId];
    tbody.innerHTML = emptyMessageHtml;
    return;
  }

  window.ADMIN_PROGRESSIVE_TABLES[tbodyId] = {
    rowsHtml: rowsHtmlArray,
    visibleCount: Math.min(stepSize, rowsHtmlArray.length),
    colSpan: colSpan,
    stepSize: stepSize,
  };

  window.paintProgressiveTable(tbodyId);
};

window.paintProgressiveTable = function(tbodyId) {
  const tbody = document.getElementById(tbodyId);
  const state = window.ADMIN_PROGRESSIVE_TABLES[tbodyId];
  if (!tbody || !state) return;

  const total = state.rowsHtml.length;
  const visible = Math.max(state.stepSize, Math.min(state.visibleCount, total));
  state.visibleCount = visible;

  let html = state.rowsHtml.slice(0, visible).join('');

  if (total > state.stepSize) {
    const canSeeMore = visible < total;
    const canShowLess = visible > state.stepSize;
    const nextBatchCount = Math.min(state.stepSize, total - visible);

    html += `
      <tr class="bg-[#F3F6F4]/90 border-t border-[#DCE5DF]">
        <td colspan="${state.colSpan}" class="px-4 py-2.5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <span class="text-[11px] font-semibold text-[#4F695D]">
              Showing <strong class="text-[#12372C]">1–${visible}</strong> of <strong class="text-[#12372C]">${total}</strong> records
            </span>
            <div class="flex items-center gap-2">
              ${canSeeMore ? `
                <button type="button"
                        onclick="window.expandProgressiveTable('${tbodyId}')"
                        class="bg-[#1B4D3E] hover:bg-[#12372C] text-white font-semibold text-[11px] px-3.5 py-1.5 rounded-lg transition-colors cursor-pointer">
                  See More (+${nextBatchCount})
                </button>
              ` : ''}
              ${canShowLess ? `
                <button type="button"
                        onclick="window.collapseProgressiveTable('${tbodyId}')"
                        class="bg-white hover:bg-[#E8EFEA] text-[#12372C] border border-[#DCE5DF] font-semibold text-[11px] px-3.5 py-1.5 rounded-lg transition-colors cursor-pointer">
                  Show Less
                </button>
              ` : ''}
            </div>
          </div>
        </td>
      </tr>
    `;
  }

  tbody.innerHTML = html;
  if (window.lucide) window.lucide.createIcons();
};

window.expandProgressiveTable = function(tbodyId) {
  const state = window.ADMIN_PROGRESSIVE_TABLES[tbodyId];
  if (!state) return;
  state.visibleCount = Math.min(state.rowsHtml.length, state.visibleCount + state.stepSize);
  window.paintProgressiveTable(tbodyId);
};

window.collapseProgressiveTable = function(tbodyId) {
  const state = window.ADMIN_PROGRESSIVE_TABLES[tbodyId];
  if (!state) return;
  state.visibleCount = Math.min(state.stepSize, state.rowsHtml.length);
  window.paintProgressiveTable(tbodyId);
};

// =========================================================
// 1. SPA PAGE NAVIGATION & UNIVERSAL PAGINATION TRIGGER
// =========================================================
window.triggerAllTablePaginations = function(resetToFirstPage = false) {
  const pageArg = resetToFirstPage ? 1 : undefined;
  if (typeof window.applyUserTablePagination === 'function') {
    window.applyUserTablePagination(pageArg);
  }
  if (typeof window.applyFoodListingPagination === 'function') {
    window.applyFoodListingPagination(pageArg);
  }
  if (typeof window.applyAppealsPagination === 'function') {
    window.applyAppealsPagination(pageArg);
  }
  if (typeof window.applyDonationsPagination === 'function') {
    window.applyDonationsPagination(pageArg);
  }
  if (typeof window.applyPayoutsPagination === 'function') {
    window.applyPayoutsPagination(pageArg);
  }
  if (typeof window.applyAuditLogsPagination === 'function') {
    window.applyAuditLogsPagination(pageArg);
  }
};

function reinitWorkspaceScripts() {
  if (window.lucide) {
    window.lucide.createIcons();
  }
  if (document.getElementById('userGrowthChart')) {
    initDashboardCharts();
  }
  initCalendarPickers();
  bindSearchInputs();
  restorePersistedTabs();
  handleDeepLinkModals();
  setTimeout(() => window.triggerAllTablePaginations(), 30);
  setTimeout(() => window.triggerAllTablePaginations(), 140);
}

window.showSpaPageLoader = function(label = 'Loading...') {
  const loader = document.getElementById('adminSpaPageLoader');
  const textEl = document.getElementById('adminSpaPageLoaderText');
  if (textEl) textEl.textContent = label;
  if (loader) {
    loader.classList.remove('hidden');
    loader.classList.add('flex');
  }
};

window.hideSpaPageLoader = function() {
  const loader = document.getElementById('adminSpaPageLoader');
  if (loader) {
    loader.classList.add('hidden');
    loader.classList.remove('flex');
  }
};

function updateTopNavActiveState(targetUrl) {
  const path = targetUrl.split('?')[0];
  let key = 'dashboard';
  if (path.includes('/users')) key = 'users';
  else if (path.includes('/food-listing')) key = 'food-listing';
  else if (path.includes('/donations')) key = 'donations';
  else if (path.includes('/payouts')) key = 'payouts';
  else if (path.includes('/announcements')) key = 'announcements';

  document.querySelectorAll('.admin-top-nav-link').forEach(link => {
    const linkKey = link.getAttribute('data-nav-key');
    if (linkKey === key) {
      link.className = 'admin-top-nav-link flex items-center gap-2 px-3.5 py-2 rounded-lg text-xs transition-all font-semibold bg-[#3A826D] text-white shadow-sm';
    } else {
      link.className = 'admin-top-nav-link flex items-center gap-2 px-3.5 py-2 rounded-lg text-xs transition-all font-medium text-[#D8E3DD] hover:text-white hover:bg-white/5';
    }
  });
}

window.navigateAdminSpa = async function(targetUrl, pushHistory = true) {
  const workspace = document.getElementById('adminMainWorkspace');
  if (!workspace) {
    window.location.href = targetUrl;
    return;
  }

  document.querySelectorAll('.modal-overlay').forEach(m => {
    m.classList.add('hidden');
    m.classList.remove('flex');
  });
  document.body.classList.remove('overflow-hidden');
  const notifMenu = document.getElementById('notifDropdown');
  const adminMenu = document.getElementById('adminDropdown');
  const mobileNav = document.getElementById('mainNavbar');
  if (notifMenu) notifMenu.classList.add('hidden');
  if (adminMenu) adminMenu.classList.add('hidden');
  if (mobileNav && window.innerWidth < 1024) mobileNav.classList.add('hidden');

  window.showSpaPageLoader('Loading...');
  try {
    const res = await fetch(targetUrl, {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    });
    if (!res.ok) {
      window.location.href = targetUrl;
      return;
    }

    const htmlText = await res.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(htmlText, 'text/html');
    const newMain = doc.getElementById('adminMainWorkspace') || doc.querySelector('main');

    if (!newMain) {
      window.location.href = targetUrl;
      return;
    }

    const newBody = doc.body;
    if (newBody) {
      ['chartLabels', 'chartCounts', 'donutCounts'].forEach(attr => {
        if (newBody.dataset[attr]) {
          document.body.dataset[attr] = newBody.dataset[attr];
        }
      });
    }

    workspace.innerHTML = newMain.innerHTML;
    if (doc.title) document.title = doc.title;

    workspace.querySelectorAll('script').forEach(oldScript => {
      const newScript = document.createElement('script');
      if (oldScript.src) {
        newScript.src = oldScript.src;
      } else {
        newScript.textContent = oldScript.textContent;
      }
      oldScript.parentNode.replaceChild(newScript, oldScript);
    });

    if (pushHistory) {
      window.history.pushState({ spaUrl: targetUrl }, doc.title || '', targetUrl);
    }

    updateTopNavActiveState(targetUrl);
    window.scrollTo({ top: 0, behavior: 'smooth' });
    reinitWorkspaceScripts();
  } catch (err) {
    window.location.href = targetUrl;
  } finally {
    window.hideSpaPageLoader();
  }
};

function initSpaNavigation() {
  document.addEventListener('click', (e) => {
    const anchor = e.target.closest('a');
    if (!anchor) return;

    const href = anchor.getAttribute('href') || '';
    if (
      !href ||
      href.startsWith('#') ||
      href.startsWith('http') ||
      href.startsWith('mailto:') ||
      href.startsWith('tel:') ||
      anchor.getAttribute('target') === '_blank'
    ) {
      return;
    }

    const isSpaRoute =
      anchor.getAttribute('data-spa-link') === 'true' ||
      href.startsWith('/dashboard/') ||
      href.startsWith('/users/') ||
      href.startsWith('/food-listing/') ||
      href.startsWith('/donations/') ||
      href.startsWith('/payouts/') ||
      href.startsWith('/announcements/');

    if (isSpaRoute) {
      e.preventDefault();
      window.navigateAdminSpa(href, true);
    }
  });

  window.addEventListener('popstate', () => {
    const token = localStorage.getItem('admin_access_token');
    if (!token) {
      window.location.replace('/login/');
      return;
    }
    const target = window.location.pathname + window.location.search;
    window.navigateAdminSpa(target, false);
  });
}

// =========================================================
// 1B. SILENT BACKGROUND AUTO-SYNC (NO PAGE REFRESH REQUIRED)
// =========================================================
function isAdminWorkspaceIdleForSync() {
  if (document.hidden) return false;
  const openModal = document.querySelector('.modal-overlay:not(.hidden)');
  if (openModal) return false;

  const activeEl = document.activeElement;
  if (activeEl && ['INPUT', 'TEXTAREA', 'SELECT'].includes(activeEl.tagName)) {
    return false;
  }

  const filterInputs = document.querySelectorAll(
    '#userManageSearchInput, #userManageDateInput, #foodListingSearchInput, #foodListingDateInput, #donationHubSearchInput, #donationHubDateInput, #payoutSearchInput, #payoutDateInput, #auditLogSearchInput, #announcementSearchInput'
  );
  for (const inp of filterInputs) {
    if ((inp.value || '').trim().length > 0) return false;
  }
  return true;
}

async function runSilentAdminAutoSync() {
  if (!isAdminWorkspaceIdleForSync()) return;

  const path = window.location.pathname || '';
  if (path.includes('/login') || path.includes('/reset-password')) return;

  try {
    const targetUrl = window.location.pathname + window.location.search;
    const res = await fetch(targetUrl, {
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'X-Admin-Auto-Sync': '1',
      },
      cache: 'no-store',
    });
    if (!res.ok) return;

    const htmlText = await res.text();
    if (!isAdminWorkspaceIdleForSync()) return;

    const parser = new DOMParser();
    const doc = parser.parseFromString(htmlText, 'text/html');

    // 1. Sync topbar notifications list & pending merchant alert
    const newNotifList = doc.getElementById('liveNotifDropdownList');
    const curNotifList = document.getElementById('liveNotifDropdownList');
    if (newNotifList && curNotifList && newNotifList.innerHTML.trim() !== curNotifList.innerHTML.trim()) {
      curNotifList.innerHTML = newNotifList.innerHTML;
    }
    const newPendingBanner = doc.getElementById('pendingMerchantsNotifBanner');
    const curPendingBanner = document.getElementById('pendingMerchantsNotifBanner');
    if (newPendingBanner && curPendingBanner && newPendingBanner.innerHTML.trim() !== curPendingBanner.innerHTML.trim()) {
      curPendingBanner.innerHTML = newPendingBanner.innerHTML;
    }
    window.syncAdminNotificationBadge();

    // 2. Sync live tables on Users, Food Listing, Donations, Payouts, and Dashboard without losing pagination state
    const tableIds = [
      'table-consumer',
      'table-merchant',
      'table-foodbank',
      'foodListingsTable',
      'appealsTable',
      'donationsTable',
      'payoutsTable',
      'dashboardRecentMarketplaceTable',
      'systemAuditLogsTable',
    ];

    let anyTableChanged = false;
    tableIds.forEach((tId) => {
      const curTbody = document.querySelector(`#${tId} tbody`);
      const newTbody = doc.querySelector(`#${tId} tbody`);
      if (!curTbody || !newTbody) return;

      const curRows = curTbody.querySelectorAll('tr');
      const newRows = newTbody.querySelectorAll('tr');

      const curSignature = `${curRows.length}:${curRows[0]?.id || ''}:${curRows[0]?.getAttribute('data-status') || ''}`;
      const newSignature = `${newRows.length}:${newRows[0]?.id || ''}:${newRows[0]?.getAttribute('data-status') || ''}`;

      if (curSignature !== newSignature) {
        curTbody.innerHTML = newTbody.innerHTML;
        anyTableChanged = true;
      }
    });

    // Sync Partner Merchant / Food Bank directory grids if new cards appeared
    ['merchantProfilesGrid'].forEach((gridId) => {
      const curGrid = document.getElementById(gridId);
      const newGrid = doc.getElementById(gridId);
      if (curGrid && newGrid && curGrid.children.length !== newGrid.children.length) {
        curGrid.innerHTML = newGrid.innerHTML;
        anyTableChanged = true;
      }
    });

    if (anyTableChanged) {
      if (document.getElementById('table-consumer')) {
        const currentRole = sessionStorage.getItem('reserve_admin_user_role_tab') || 'Consumer';
        updateRoleStats(currentRole);
      }
      if (window.lucide) window.lucide.createIcons();
      window.triggerAllTablePaginations(false);
    }
  } catch (e) {
    // Silent ignore on background network hiccups
  }
}

function initAdminLiveAutoSync() {
  if (window.ADMIN_AUTO_SYNC_TIMER) {
    clearInterval(window.ADMIN_AUTO_SYNC_TIMER);
  }
  window.ADMIN_AUTO_SYNC_TIMER = setInterval(runSilentAdminAutoSync, 8000);
}

// =========================================================
// 2. NON-BLOCKING ACTION LOADER & TAB PERSISTENCE
// =========================================================
window.showAdminLoader = function(text = 'Saving changes...') {
  const loader = document.getElementById('adminGlobalLoader');
  const label = document.getElementById('adminGlobalLoaderText');
  if (label) label.textContent = text;
  if (loader) loader.classList.remove('hidden');
};

window.hideAdminLoader = function() {
  const loader = document.getElementById('adminGlobalLoader');
  if (loader) loader.classList.add('hidden');
};

function restorePersistedTabs() {
  const savedRole = sessionStorage.getItem('reserve_admin_user_role_tab');
  if (savedRole && document.getElementById('table-consumer')) {
    const tabId = `role-tab-${savedRole.replace(/\s+/g, '')}`;
    const btn = document.getElementById(tabId);
    if (btn) {
      window.switchRoleTab(savedRole, btn);
    } else {
      updateRoleStats('Consumer');
    }
  } else if (document.getElementById('table-consumer')) {
    updateRoleStats('Consumer');
  }
}

function handleDeepLinkModals() {
  const params = new URLSearchParams(window.location.search);
  const merchParam = (params.get('merchant') || '').replace('MID-', '').trim();
  if (merchParam && params.get('open_profile') === '1') {
    const targetCard = document.getElementById(`merchant-profile-card-${merchParam}`);
    if (targetCard) {
      targetCard.classList.remove('hidden');
      setTimeout(() => window.openMerchantDirectoryProfile(targetCard), 150);
    }
  }
  const fbParam = (params.get('foodbank') || '').replace('FB-', '').trim();
  if (fbParam && params.get('open_profile') === '1') {
    const targetCard = document.getElementById(`foodbank-profile-card-${fbParam}`);
    if (targetCard) {
      targetCard.classList.remove('hidden');
      setTimeout(() => window.openFoodBankDirectoryProfile(targetCard), 150);
    }
  }
}

// =========================================================
// 3. DASHBOARD CHARTS & HEADER MENUS
// =========================================================
function initDashboardCharts() {
  const growthCanvas = document.getElementById('userGrowthChart');
  const donutCanvas = document.getElementById('trafficDonutChart');

  let labels = ['May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct'];
  let counts = [0, 0, 0, 0, 0, 0];
  let donutData = [1, 1, 1];

  try {
    if (document.body.dataset.chartLabels) {
      labels = JSON.parse(document.body.dataset.chartLabels);
    }
    if (document.body.dataset.chartCounts) {
      counts = JSON.parse(document.body.dataset.chartCounts);
    }
    if (document.body.dataset.donutCounts) {
      const parsedDonut = JSON.parse(document.body.dataset.donutCounts);
      if (parsedDonut.some(v => v > 0)) donutData = parsedDonut;
    }
  } catch (e) {
    console.log('Chart data parse fallback:', e);
  }

  if (growthCanvas && window.Chart) {
    if (window.ADMIN_CHART_INSTANCES.growth) {
      window.ADMIN_CHART_INSTANCES.growth.destroy();
    }
    window.ADMIN_CHART_INSTANCES.growth = new Chart(growthCanvas, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: 'New Platform Registrations',
          data: counts,
          borderColor: '#1B4D3E',
          backgroundColor: 'rgba(58, 130, 109, 0.14)',
          borderWidth: 3,
          fill: true,
          tension: 0.35,
          pointBackgroundColor: '#1B4D3E',
          pointRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, ticks: { precision: 0 } }
        }
      }
    });
  }

  if (donutCanvas && window.Chart) {
    if (window.ADMIN_CHART_INSTANCES.donut) {
      window.ADMIN_CHART_INSTANCES.donut.destroy();
    }
    window.ADMIN_CHART_INSTANCES.donut = new Chart(donutCanvas, {
      type: 'doughnut',
      data: {
        labels: ['Bakeshops', 'Market Vendors', 'Others'],
        datasets: [{
          data: donutData,
          backgroundColor: ['#1B4D3E', '#3A826D', '#EBB338'],
          borderWidth: 0
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '70%',
        plugins: { legend: { display: false } }
      }
    });
  }
}

window.toggleAdminMenu = function() {
  const menu = document.getElementById('adminDropdown');
  const notifMenu = document.getElementById('notifDropdown');
  if (notifMenu) notifMenu.classList.add('hidden');
  if (menu) menu.classList.toggle('hidden');
};

window.toggleMobileNav = function() {
  const nav = document.getElementById('mainNavbar');
  if (nav) nav.classList.toggle('hidden');
};

// =========================================================
// 4. CATEGORIZED NOTIFICATIONS, OPENED vs. UNREAD STYLING & ROUTING
// =========================================================
function classifyAdminNotifCategory(typeStr, msgStr) {
  const t = (typeStr || '').toLowerCase();
  const m = (msgStr || '').toLowerCase();
  if (t.includes('compliance') || m.includes('no-show') || m.includes('strike') || m.includes('suspend') || m.includes('flag')) {
    return 'compliance';
  }
  if (t.includes('kyb') || t.includes('registration') || t.includes('approved') || m.includes('permit') || m.includes('application')) {
    return 'kyb';
  }
  if (t.includes('payout') || t.includes('reservation') || m.includes('withdrawal') || m.includes('po-') || m.includes('oid')) {
    return 'orders';
  }
  if (t.includes('donation') || t.includes('listing') || m.includes('appeal') || m.includes('posted')) {
    return 'donations';
  }
  return 'all';
}

function applyNotifRowVisualState(item, isRead) {
  const dot = item.querySelector('.notif-unread-dot');
  const stateLabel = item.querySelector('.notif-state-label');
  const msg = (item.getAttribute('data-notif-msg') || '');
  const isUrgent = msg.includes('3-STRIKE') || msg.includes('No-Show');

  if (isRead) {
    if (dot) dot.classList.add('hidden');
    if (stateLabel) {
      stateLabel.textContent = 'Opened ✓';
      stateLabel.className = 'notif-state-label text-[9px] font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-[#4F695D]';
    }
    item.classList.remove('bg-[#E8EFEA]', 'border-[#3A826D]/40', 'bg-[#FBE4E1]/70', 'border-[#B3261E]/40', 'bg-[#FEF3C7]', 'border-amber-300');
    item.classList.add('bg-white', 'border-[#DCE5DF]', 'opacity-75');
  } else {
    if (dot) dot.classList.remove('hidden');
    if (stateLabel) {
      stateLabel.textContent = 'NEW';
      stateLabel.className = 'notif-state-label text-[9px] font-bold px-1.5 py-0.5 rounded bg-white/90 text-[#1B4D3E]';
    }
    item.classList.remove('bg-white', 'border-[#DCE5DF]', 'opacity-75');
    if (isUrgent) {
      item.classList.add('bg-[#FBE4E1]/70', 'border-[#B3261E]/40');
    } else {
      item.classList.add('bg-[#E8EFEA]', 'border-[#3A826D]/40');
    }
  }
}

window.syncAdminNotificationBadge = function() {
  const badge = document.getElementById('notifBadgeCount');
  const statusPill = document.getElementById('notifReadStatusPill');
  const items = Array.from(document.querySelectorAll('.admin-notif-item'));
  if (!badge) return;

  let seenIds = [];
  try {
    seenIds = JSON.parse(localStorage.getItem('admin_seen_notif_ids') || '[]');
  } catch (e) {
    seenIds = [];
  }

  let unreadCount = 0;
  items.forEach(item => {
    const id = item.getAttribute('data-notif-id');
    const isRead = Boolean(id && seenIds.includes(id));
    if (!isRead) unreadCount++;
    applyNotifRowVisualState(item, isRead);
  });

  if (unreadCount > 0) {
    badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
    badge.classList.remove('hidden');
    badge.classList.add('flex');
    if (statusPill) statusPill.textContent = `${unreadCount} Unread`;
  } else {
    badge.classList.add('hidden');
    badge.classList.remove('flex');
    if (statusPill) statusPill.textContent = 'All Opened ✓';
  }
};

window.filterAdminNotifsByCategory = function(category, btnEl) {
  document.querySelectorAll('.notif-cat-btn').forEach(btn => {
    btn.className = 'notif-cat-btn px-2.5 py-1 rounded-lg text-[10px] font-medium bg-[#F7F9F7] text-[#4F695D] border border-[#DCE5DF] hover:text-[#12372C] shrink-0';
  });
  if (btnEl) {
    btnEl.className = 'notif-cat-btn px-2.5 py-1 rounded-lg text-[10px] font-semibold bg-[#1B4D3E] text-white shrink-0';
  }

  document.querySelectorAll('.admin-notif-item').forEach(item => {
    const nType = item.getAttribute('data-notif-type') || '';
    const nMsg = item.getAttribute('data-notif-msg') || '';
    const itemCat = classifyAdminNotifCategory(nType, nMsg);
    if (category === 'all' || itemCat === category) {
      item.style.display = '';
    } else {
      item.style.display = 'none';
    }
  });
};

window.markAllAdminNotificationsRead = function() {
  const items = Array.from(document.querySelectorAll('.admin-notif-item'));
  let seenIds = [];
  try {
    seenIds = JSON.parse(localStorage.getItem('admin_seen_notif_ids') || '[]');
  } catch (e) {
    seenIds = [];
  }
  items.forEach(item => {
    const id = item.getAttribute('data-notif-id');
    if (id && !seenIds.includes(id)) seenIds.push(id);
    applyNotifRowVisualState(item, true);
  });
  localStorage.setItem('admin_seen_notif_ids', JSON.stringify(seenIds));
  window.syncAdminNotificationBadge();
};

window.toggleNotifMenu = function() {
  const menu = document.getElementById('notifDropdown');
  const adminMenu = document.getElementById('adminDropdown');
  if (adminMenu) adminMenu.classList.add('hidden');
  if (!menu) return;

  menu.classList.toggle('hidden');
  window.syncAdminNotificationBadge();
};

window.handleAdminNotifClick = function(el) {
  const notifId = el.getAttribute('data-notif-id');
  if (notifId) {
    let seenIds = [];
    try {
      seenIds = JSON.parse(localStorage.getItem('admin_seen_notif_ids') || '[]');
    } catch (e) {
      seenIds = [];
    }
    if (!seenIds.includes(notifId)) {
      seenIds.push(notifId);
      localStorage.setItem('admin_seen_notif_ids', JSON.stringify(seenIds));
    }
    applyNotifRowVisualState(el, true);
    window.syncAdminNotificationBadge();
  }

  const nType = (el.getAttribute('data-notif-type') || '').toLowerCase();
  const nMsg = (el.getAttribute('data-notif-msg') || '').toLowerCase();

  if (nType.includes('payout') || nMsg.includes('withdrawal') || nMsg.includes('po-')) {
    window.navigateAdminSpa('/payouts/');
    return;
  }
  if (nType.includes('listing') || nMsg.includes('posted') || nMsg.includes('listing')) {
    window.navigateAdminSpa('/food-listing/');
    return;
  }
  if (nType.includes('donation') || nMsg.includes('appeal') || nMsg.includes('donation')) {
    window.navigateAdminSpa('/donations/');
    return;
  }
  if (nType.includes('broadcast') || nType.includes('announcement')) {
    window.navigateAdminSpa('/announcements/');
    return;
  }
  if (nMsg.includes('food bank') || nMsg.includes('fb-')) {
    sessionStorage.setItem('reserve_admin_user_role_tab', 'Food Bank');
    window.navigateAdminSpa('/users/');
    return;
  }
  if (nMsg.includes('consumer') || nMsg.includes('cid-') || nMsg.includes('no-show') || nMsg.includes('strike') || nType.includes('reservation')) {
    sessionStorage.setItem('reserve_admin_user_role_tab', 'Consumer');
    window.navigateAdminSpa('/users/');
    return;
  }
  sessionStorage.setItem('reserve_admin_user_role_tab', 'Merchant');
  window.navigateAdminSpa('/users/');
};

// =========================================================
// 5. ENTERPRISE ADMIN SETTINGS, GOVERNANCE & MASTER CSV EXPORT
// =========================================================
window.openAdminProfile = function(defaultTab = 'account') {
  const adminMenu = document.getElementById('adminDropdown');
  if (adminMenu) adminMenu.classList.add('hidden');
  const tabBtn = document.getElementById(`adminSetTabBtn-${defaultTab}`);
  if (tabBtn) window.switchAdminSettingsTab(defaultTab, tabBtn);
  window.openModal('adminProfileModal');
};

window.switchAdminSettingsTab = function(tabKey, btnEl) {
  document.querySelectorAll('.admin-settings-tab').forEach(btn => {
    btn.className = 'admin-settings-tab px-4 py-2 text-xs font-medium border-b-2 border-transparent text-[#4F695D] hover:text-[#12372C]';
  });
  btnEl.className = 'admin-settings-tab px-4 py-2 text-xs font-semibold border-b-2 border-[#1B4D3E] text-[#1B4D3E]';

  document.querySelectorAll('.admin-settings-panel').forEach(p => p.classList.add('hidden'));
  const panel = document.getElementById(`adminSetPanel-${tabKey}`);
  if (panel) panel.classList.remove('hidden');
};

window.saveAdminProfile = async function() {
  const emailInput = document.getElementById('adminProfileEmailInput');
  if (!emailInput || !emailInput.value.trim()) return;

  window.showAdminLoader('Updating admin work email...');
  try {
    const res = await fetch('/api/accounts/admin/profile/update/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({ action: 'update_profile', email: emailInput.value.trim() })
    });
    const data = await res.json().catch(() => ({}));
    window.hideAdminLoader();
    if (res.ok) {
      await themeAlert('Work Email Updated', data.message || 'Admin email updated in database!', true);
    } else {
      await themeAlert('Update Failed', data.error || 'Failed to update admin email.', false);
    }
  } catch (e) {
    window.hideAdminLoader();
    await themeAlert('Network Error', 'Network error while saving profile.', false);
  }
};

window.changeAdminPassword = async function() {
  const curPw = document.getElementById('adminCurrentPwInput')?.value || '';
  const newPw = document.getElementById('adminNewPwInput')?.value || '';
  const confPw = document.getElementById('adminConfirmPwInput')?.value || '';

  if (newPw.length < 6) {
    await themeAlert('Weak Password', 'New password must be at least 6 characters long.', false);
    return;
  }
  if (newPw !== confPw) {
    await themeAlert('Password Mismatch', 'New Password and Confirm Password do not match.', false);
    return;
  }

  window.showAdminLoader('Updating administrator password...');
  try {
    const res = await fetch('/api/accounts/admin/profile/update/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({
        action: 'change_password',
        current_password: curPw,
        new_password: newPw,
      })
    });
    const data = await res.json().catch(() => ({}));
    window.hideAdminLoader();
    if (res.ok) {
      document.getElementById('adminCurrentPwInput').value = '';
      document.getElementById('adminNewPwInput').value = '';
      document.getElementById('adminConfirmPwInput').value = '';
      await themeAlert('Password Updated', data.message || 'Your administrator password has been updated.', true);
    } else {
      await themeAlert('Update Failed', data.error || 'Could not update password.', false);
    }
  } catch (e) {
    window.hideAdminLoader();
    await themeAlert('Network Error', 'Could not reach the server.', false);
  }
};

window.savePlatformGovernanceSettings = async function() {
  const strikeLimit = document.getElementById('govStrikeLimit')?.value || 3;
  const radiusKm = document.getElementById('govRadiusKm')?.value || 10;
  const freeCap = document.getElementById('govFreeCap')?.value || 10;
  const commissionPct = document.getElementById('govCommissionPct')?.value || 10;
  const premiumPrice = document.getElementById('govPremiumPrice')?.value || 149;

  window.showAdminLoader('Saving governance thresholds...');
  try {
    const res = await fetch('/api/accounts/admin/profile/update/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({
        action: 'save_governance',
        strike_limit: strikeLimit,
        radius_km: radiusKm,
        free_cap: freeCap,
        commission_pct: commissionPct,
        premium_monthly_price: premiumPrice,
      })
    });
    const data = await res.json().catch(() => ({}));
    window.hideAdminLoader();
    if (res.ok) {
      await themeAlert('Governance Saved', data.message || 'Platform thresholds updated!', true);
    } else {
      await themeAlert('Save Failed', data.error || 'Could not update governance settings.', false);
    }
  } catch (e) {
    window.hideAdminLoader();
    await themeAlert('Network Error', 'Could not reach the server.', false);
  }
};

window.resetAdminReadNotifications = async function() {
  localStorage.removeItem('admin_seen_notif_ids');
  window.syncAdminNotificationBadge();
  await themeAlert('Unread Badge Reset', 'All operational notifications have been marked as unread ("NEW").', true);
};

window.exportActiveTableToCSV = async function() {
  window.showAdminLoader('Compiling Master CSV (Users, Listings, Donations & Payouts)...');
  try {
    const res = await fetch('/api/accounts/admin/portal-action/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({ module: 'export_all_csv', action: 'export' })
    });
    const data = await res.json().catch(() => ({}));
    window.hideAdminLoader();

    if (res.ok && Array.isArray(data.rows)) {
      const csvLines = data.rows.map(row =>
        row.map(cell => `"${String(cell ?? '').replace(/"/g, '""').replace(/\s+/g, ' ').trim()}"`).join(',')
      );
      const blob = new Blob(['\uFEFF' + csvLines.join('\n')], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `reserve_master_audit_export_${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      await themeAlert('Master CSV Exported', 'Successfully exported Users, Food Listings, Donations, and Payouts into a single master CSV file!', true);
      return;
    }
  } catch (err) {
    window.hideAdminLoader();
  }

  const tables = Array.from(document.querySelectorAll('table'));
  if (!tables.length) return;
  const csvLines = [];
  tables.forEach((table, idx) => {
    csvLines.push(`"=== TABLE ${idx + 1}: ${table.id || 'RECORDS'} ==="`);
    Array.from(table.querySelectorAll('tr')).forEach(row => {
      const cells = Array.from(row.querySelectorAll('th, td')).slice(0, -1);
      csvLines.push(cells.map(c => `"${(c.innerText || '').replace(/"/g, '""').replace(/\s+/g, ' ').trim()}"`).join(','));
    });
    csvLines.push('');
  });

  const blob = new Blob(['\uFEFF' + csvLines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `reserve_admin_export_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
};

window.handleLogout = async function() {
  const isSure = await themeConfirm(
    'Confirm Secure Logout',
    'Are you sure you want to log out of the ReServe Enterprise Console?'
  );
  if (!isSure) return;

  window.showAdminLoader('Terminating session...');
  try {
    await fetch('/api/accounts/admin/profile/update/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({ action: 'logout' })
    });
  } catch (e) {}

  localStorage.removeItem('admin_access_token');
  localStorage.removeItem('admin_refresh_token');
  sessionStorage.clear();
  window.hideAdminLoader();
  window.location.replace('/login/');
};

// =========================================================
// 6. 360° DOSSIER, PROGRESSIVE 10-ROW TABLES & DIGITAL RECEIPTS
// =========================================================
window.switchDossierTab = function(prefix, panelKey, btnEl) {
  document.querySelectorAll(`.${prefix}-dossier-tab`).forEach(t => {
    t.className = `${prefix}-dossier-tab px-4 py-2 text-[11px] font-bold rounded-xl bg-white text-gray-600 hover:text-[#1B4D3E]`;
  });
  btnEl.className = `${prefix}-dossier-tab px-4 py-2 text-[11px] font-black rounded-xl bg-[#1B4D3E] text-white`;

  document.querySelectorAll(`.${prefix}-dossier-panel`).forEach(p => p.classList.add('hidden'));
  const target = document.getElementById(`${prefix}-panel-${panelKey}`);
  if (target) target.classList.remove('hidden');
};

window.openDigitalReceiptModal = function(txId) {
  const tx = window.ADMIN_TX_CACHE[txId];
  if (!tx) return;

  document.getElementById('drModalId').textContent = `Digital Receipt — ${tx.id}`;
  document.getElementById('drModalType').textContent = tx.receipt_type || 'Official System Transaction & Handshake';
  document.getElementById('drModalVoucher').textContent = tx.id;
  document.getElementById('drModalGatewayRef').textContent = `Ref: ${tx.gateway_ref || 'VERIFIED-' + tx.id}`;
  document.getElementById('drModalMerchant').textContent = tx.establishment || 'Partner Merchant';
  document.getElementById('drModalMerchantPhone').textContent = `Phone: ${tx.merchant_phone || 'Verified Store'}`;
  document.getElementById('drModalAddress').textContent = tx.address || 'Cebu City';

  document.getElementById('drModalBuyer').textContent = tx.buyer || 'Verified Recipient';
  document.getElementById('drModalBuyerPhone').textContent = `Phone: ${tx.buyer_phone || 'Verified'}`;
  document.getElementById('drModalBuyerEmail').textContent = tx.buyer_email || '';

  document.getElementById('drModalDate').textContent = tx.date || 'N/A';
  document.getElementById('drModalDeadline').textContent = tx.pickup_deadline || 'Standard Window';
  document.getElementById('drModalTotal').textContent = tx.total_value || tx.amount || '₱0.00';
  document.getElementById('drModalDeposit').textContent = tx.deposit_paid || '₱0.00';
  document.getElementById('drModalRemaining').textContent = tx.remaining_balance || '₱0.00';

  const statusBadge = document.getElementById('drModalStatusBadge');
  const st = (tx.status || 'Active').toUpperCase();
  statusBadge.textContent = st;
  if (st.includes('NO-SHOW') || st.includes('REJECT') || st.includes('CANCEL') || st.includes('FLAG')) {
    statusBadge.className = 'bg-[#FBE4E1] text-[#B3261E] px-3 py-1 rounded-lg text-[10px] font-semibold uppercase';
  } else if (st.includes('PEND') || st.includes('ONGOING')) {
    statusBadge.className = 'bg-[#FEF3C7] text-[#B45309] px-3 py-1 rounded-lg text-[10px] font-semibold uppercase';
  } else {
    statusBadge.className = 'bg-[#E4F0E8] text-[#1F6B52] px-3 py-1 rounded-lg text-[10px] font-semibold uppercase';
  }

  const itemsContainer = document.getElementById('drModalItemsList');
  const items = tx.items_breakdown || [{ name: tx.title, qty: 1, unit_price: tx.total_value || tx.amount, subtotal: tx.total_value || tx.amount }];
  itemsContainer.innerHTML = items.map(it => `
    <div class="flex justify-between items-center bg-[#F7F9F7] px-3 py-2 rounded-xl border border-[#DCE5DF]">
      <div>
        <span class="font-semibold text-[#12372C]">${it.qty}x ${it.name}</span>
        <span class="text-[10px] text-[#4F695D] block">${it.category || 'Surplus'} • Unit: ${it.unit_price}</span>
      </div>
      <span class="font-bold text-[#1B4D3E]">${it.subtotal}</span>
    </div>
  `).join('');

  window.openModal('adminDigitalReceiptModal');
};

function statusBadgeHtml(statusStr) {
  const s = (statusStr || 'Active').toLowerCase();
  if (s.includes('no-show') || s.includes('reject') || s.includes('suspend') || s.includes('cancel') || s.includes('withdrawn') || s.includes('flag')) {
    return `<span class="bg-[#FBE4E1] text-[#B3261E] px-2.5 py-1 rounded-lg text-[10px] font-semibold">${statusStr}</span>`;
  }
  if (s.includes('ongoing') || s.includes('claim')) {
    return `<span class="bg-[#E1EEF8] text-[#2C5F8A] px-2.5 py-1 rounded-lg text-[10px] font-semibold">${statusStr}</span>`;
  }
  if (s.includes('pend')) {
    return `<span class="bg-[#FEF3C7] text-[#B45309] px-2.5 py-1 rounded-lg text-[10px] font-semibold">${statusStr}</span>`;
  }
  return `<span class="bg-[#E4F0E8] text-[#1F6B52] px-2.5 py-1 rounded-lg text-[10px] font-semibold">${statusStr}</span>`;
}

async function fetchUserDossierWithFallback(role, accountId) {
  try {
    const res1 = await fetch(`/api/accounts/admin/dossier/${role}/${accountId}/`);
    if (res1.ok) {
      return await res1.json();
    }
  } catch (e) {}

  const res2 = await fetch(`/api/accounts/admin/user-transactions/${role}/${accountId}/`);
  if (res2.ok) {
    return await res2.json();
  }
  return null;
}

async function loadAdminUserDossier(role, accountId) {
  try {
    const data = await fetchUserDossierWithFallback(role, accountId);
    if (!data) return;

    const b = data.behavior || {};
    const txs = data.transactions || [];
    const revs = data.reviews || [];
    const repsRec = data.reports_received || [];
    const issRep = data.issues_reported || [];

    txs.forEach(t => { window.ADMIN_TX_CACHE[t.id] = t; });

    if (role === 'Consumer') {
      const strikes = Number(b.no_show_strikes || 0);
      const limit = Number(b.strike_limit || document.getElementById('govStrikeLimit')?.value || 3);
      const isFlaggedForSuspension = Boolean(b.suspension_recommended || strikes >= limit);

      const strikeEl = document.getElementById('consMetricStrikes');
      if (strikeEl) {
        strikeEl.textContent = `${strikes} / ${limit}`;
        strikeEl.className = isFlaggedForSuspension
          ? 'text-2xl font-bold text-[#B3261E] mt-1 animate-pulse'
          : 'text-2xl font-bold text-[#12372C] mt-1';
      }
      const alertBanner = document.getElementById('consStrikeAlertBanner');
      if (alertBanner) {
        if (isFlaggedForSuspension) alertBanner.classList.remove('hidden');
        else alertBanner.classList.add('hidden');
      }

      if (document.getElementById('consMetricCompleted')) {
        document.getElementById('consMetricCompleted').textContent = b.completed_orders ?? 0;
      }
      if (document.getElementById('consMetricTotalOrders')) {
        document.getElementById('consMetricTotalOrders').textContent = `of ${b.total_orders ?? txs.length} total orders`;
      }
      if (document.getElementById('consMetricSavedKg')) {
        document.getElementById('consMetricSavedKg').textContent = b.total_food_saved_kg || '0.0 kg';
      }
      if (document.getElementById('consMetricRating')) {
        document.getElementById('consMetricRating').textContent = `★ ${b.avg_rating_given ?? 0} (${b.reviews_count ?? revs.length})`;
      }
      if (document.getElementById('consMetricReportsRec')) {
        document.getElementById('consMetricReportsRec').textContent = b.reports_received_count ?? repsRec.length;
      }
      if (document.getElementById('consMetricIssuesRep')) {
        document.getElementById('consMetricIssuesRep').textContent = b.issues_reported_count ?? issRep.length;
      }

      const rowStrings = txs.map(tx => `
        <tr class="hover:bg-[#F7F9F7]">
          <td class="p-3 font-mono font-semibold text-[#1B4D3E]">${tx.id}</td>
          <td class="p-3 font-semibold text-[#12372C]">${tx.title}</td>
          <td class="p-3 text-[#4F695D]">${tx.establishment}</td>
          <td class="p-3 text-[#4F695D]">${tx.date}</td>
          <td class="p-3">
            <span class="font-bold text-[#12372C] block">${tx.total_value || tx.amount}</span>
            <span class="text-[10px] text-[#3A826D] font-medium block">20% Online: ${tx.deposit_paid || '₱0.00'} • 80% Cash: ${tx.remaining_balance || '₱0.00'}</span>
          </td>
          <td class="p-3">${statusBadgeHtml(tx.status)}</td>
          <td class="p-3">
            <button type="button" onclick="openDigitalReceiptModal('${tx.id}')" class="bg-[#3A826D] hover:bg-[#1B4D3E] text-white font-semibold text-[10px] px-3 py-1.5 rounded-lg transition-colors cursor-pointer">
              Receipt
            </button>
          </td>
        </tr>
      `);

      window.renderProgressiveRows(
        'consumerTransactionsBody',
        rowStrings,
        `<tr><td colspan="7" class="p-4 text-center text-[#4F695D]">No reservations recorded yet.</td></tr>`,
        7,
        10
      );

      const revBox = document.getElementById('consumerReviewsBody');
      if (revBox) {
        revBox.innerHTML = revs.length === 0
          ? `<p class="text-center text-[#4F695D] py-4">No ratings or reviews submitted yet.</p>`
          : revs.map(r => `
            <div class="p-3 bg-[#F7F9F7] rounded-xl border border-[#DCE5DF] flex justify-between items-start gap-3">
              <div>
                <p class="font-semibold text-[#12372C]">${r.item} — <span class="text-[#4F695D]">${r.merchant}</span> (${r.voucher})</p>
                <p class="text-amber-600 font-bold text-xs mt-0.5">${'★'.repeat(r.score)} (${r.score}/5)</p>
                <p class="text-[#4F695D] mt-1 italic">"${r.comment}"</p>
              </div>
              <span class="text-[10px] text-[#4F695D] shrink-0">${r.date}</span>
            </div>
          `).join('');
      }

      const repBox = document.getElementById('consumerReportsReceivedBody');
      if (repBox) {
        repBox.innerHTML = repsRec.length === 0
          ? `<p class="text-center text-[#4F695D] py-4">Clean record! Zero No-Show strikes or compliance reports against this consumer.</p>`
          : repsRec.map(rp => `
            <div class="p-3 bg-[#FBE4E1]/50 rounded-xl border border-[#B3261E]/30 flex justify-between items-start gap-3">
              <div>
                <span class="text-[9px] font-semibold uppercase bg-[#B3261E] text-white px-2 py-0.5 rounded-md">${rp.type}</span>
                <p class="text-[#12372C] font-medium mt-1">${rp.message}</p>
              </div>
              <span class="text-[10px] text-[#4F695D] shrink-0">${rp.date}</span>
            </div>
          `).join('');
      }

      const issBox = document.getElementById('consumerIssuesReportedBody');
      if (issBox) {
        issBox.innerHTML = issRep.length === 0
          ? `<p class="text-center text-[#4F695D] py-4">No Help &amp; Support issues reported by this consumer.</p>`
          : issRep.map(is => `
            <div class="p-3 bg-[#F7F9F7] rounded-xl border border-[#DCE5DF] flex justify-between items-start gap-3">
              <div>
                <span class="text-[9px] font-semibold uppercase bg-[#1B4D3E] text-white px-2 py-0.5 rounded-md">${is.type}</span>
                <p class="text-[#12372C] font-medium mt-1">${is.message}</p>
              </div>
              <span class="text-[10px] text-[#4F695D] shrink-0">${is.date}</span>
            </div>
          `).join('');
      }
    } else if (role === 'Merchant') {
      if (document.getElementById('merchMetricRating')) {
        document.getElementById('merchMetricRating').textContent = `★ ${b.avg_rating ?? 0} (${b.reviews_count ?? revs.length})`;
      }
      if (document.getElementById('merchMetricActive')) {
        document.getElementById('merchMetricActive').textContent = b.active_listings ?? 0;
      }
      if (document.getElementById('merchMetricSales')) {
        document.getElementById('merchMetricSales').textContent = b.completed_sales ?? txs.filter(t => t.status === 'Completed').length;
      }
      if (document.getElementById('merchMetricDonations')) {
        document.getElementById('merchMetricDonations').textContent = b.donations_posted ?? txs.filter(t => String(t.id).startsWith('DON-')).length;
      }
      if (document.getElementById('merchMetricWallet')) {
        document.getElementById('merchMetricWallet').textContent = b.wallet_balance || '₱0.00';
      }
      if (document.getElementById('merchMetricFlags')) {
        document.getElementById('merchMetricFlags').textContent = `${b.reports_received_count ?? repsRec.length} / ${b.issues_reported_count ?? issRep.length}`;
      }

      const merchRows = txs.map(tx => {
        const isDon = String(tx.id || '').startsWith('DON-');
        const subText = isDon
          ? `<span class="text-[10px] text-[#4F695D] block">Charitable Handshake</span>`
          : `<span class="text-[10px] text-[#3A826D] font-medium block">20% Online: ${tx.deposit_paid || '₱0.00'} • 80% Cash: ${tx.remaining_balance || '₱0.00'}</span>`;
        return `
          <tr class="hover:bg-[#F7F9F7]">
            <td class="p-3 font-mono font-semibold text-[#1B4D3E]">${tx.id}</td>
            <td class="p-3 font-semibold text-[#12372C]">${tx.title}</td>
            <td class="p-3 text-[#4F695D]">${tx.buyer}</td>
            <td class="p-3 text-[#4F695D]">${tx.date}</td>
            <td class="p-3">
              <span class="font-bold text-[#12372C] block">${tx.total_value || tx.amount}</span>
              ${subText}
            </td>
            <td class="p-3">${statusBadgeHtml(tx.status)}</td>
            <td class="p-3">
              <button type="button" onclick="openDigitalReceiptModal('${tx.id}')" class="bg-[#3A826D] hover:bg-[#1B4D3E] text-white font-semibold text-[10px] px-3 py-1.5 rounded-lg transition-colors cursor-pointer">
                Receipt
              </button>
            </td>
          </tr>
        `;
      });

      window.renderProgressiveRows(
        'merchantTransactionsBody',
        merchRows,
        `<tr><td colspan="7" class="p-4 text-center text-[#4F695D]">No sales or donation handshakes recorded yet.</td></tr>`,
        7,
        10
      );

      const revBox = document.getElementById('merchantReviewsBody');
      if (revBox) {
        revBox.innerHTML = revs.length === 0
          ? `<p class="text-center text-[#4F695D] py-4">No consumer reviews received yet.</p>`
          : revs.map(r => `
            <div class="p-3 bg-[#F7F9F7] rounded-xl border border-[#DCE5DF] flex justify-between items-start gap-3">
              <div>
                <p class="font-semibold text-[#12372C]">${r.consumer} rated ${r.item} (${r.voucher})</p>
                <p class="text-amber-600 font-bold text-xs mt-0.5">${'★'.repeat(r.score)} (${r.score}/5)</p>
                <p class="text-[#4F695D] mt-1 italic">"${r.comment}"</p>
              </div>
              <span class="text-[10px] text-[#4F695D] shrink-0">${r.date}</span>
            </div>
          `).join('');
      }

      const repBox = document.getElementById('merchantReportsBody');
      if (repBox) {
        const combined = [...repsRec, ...issRep];
        repBox.innerHTML = combined.length === 0
          ? `<p class="text-center text-[#4F695D] py-4">No compliance flags or support tickets logged for this merchant.</p>`
          : combined.map(c => `
            <div class="p-3 bg-[#F7F9F7] rounded-xl border border-[#DCE5DF] flex justify-between items-start gap-3">
              <div>
                <span class="text-[9px] font-semibold uppercase bg-[#1B4D3E] text-white px-2 py-0.5 rounded-md">${c.type}</span>
                <p class="text-[#12372C] font-medium mt-1">${c.message}</p>
              </div>
              <span class="text-[10px] text-[#4F695D] shrink-0">${c.date}</span>
            </div>
          `).join('');
      }
    } else if (role === 'FoodBank') {
      if (document.getElementById('fbMetricRescued')) {
        document.getElementById('fbMetricRescued').textContent = b.total_rescued_units || '0 Units';
      }
      if (document.getElementById('fbMetricSuccess')) {
        document.getElementById('fbMetricSuccess').textContent = b.successful_handshakes ?? 0;
      }
      if (document.getElementById('fbMetricOngoing')) {
        document.getElementById('fbMetricOngoing').textContent = b.ongoing_handshakes ?? 0;
      }
      if (document.getElementById('fbMetricTickets')) {
        document.getElementById('fbMetricTickets').textContent = `${b.reports_received_count ?? 0} / ${b.issues_reported_count ?? 0}`;
      }

      const fbRows = txs.map(tx => `
        <tr class="hover:bg-[#F7F9F7]">
          <td class="p-3 font-mono font-semibold text-[#1B4D3E]">${tx.id}</td>
          <td class="p-3 font-semibold text-[#12372C]">${tx.establishment}</td>
          <td class="p-3 text-[#4F695D]">${tx.title} (${tx.amount})</td>
          <td class="p-3 text-[#4F695D]">${tx.date}</td>
          <td class="p-3">${statusBadgeHtml(tx.status)}</td>
          <td class="p-3">
            <button type="button" onclick="openDigitalReceiptModal('${tx.id}')" class="bg-[#3A826D] hover:bg-[#1B4D3E] text-white font-semibold text-[10px] px-3 py-1.5 rounded-lg transition-colors cursor-pointer">
              Receipt
            </button>
          </td>
        </tr>
      `);

      window.renderProgressiveRows(
        'fbTransactionsBody',
        fbRows,
        `<tr><td colspan="6" class="p-4 text-center text-[#4F695D]">No appeals or donation handshakes recorded yet.</td></tr>`,
        6,
        10
      );
    }
  } catch (err) {
    console.log('Dossier load error:', err);
  }
}

async function loadLiveUserTransactions(role, accountId, targetTbodyId, colCount = 6) {
  const tbody = document.getElementById(targetTbodyId);
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="${colCount + 1}" class="p-4 text-center text-[#4F695D] text-xs">Loading live records...</td></tr>`;

  try {
    const data = await fetchUserDossierWithFallback(role, accountId);
    const list = (data && data.transactions) || [];
    const rowStrings = list.map(tx => {
      window.ADMIN_TX_CACHE[tx.id] = tx;
      return `
        <tr class="hover:bg-[#F7F9F7]">
          <td class="p-2.5 font-mono font-semibold text-[#1B4D3E]">${tx.id}</td>
          <td class="p-2.5 font-semibold text-[#12372C]">${tx.title}</td>
          <td class="p-2.5 text-[#4F695D]">${role === 'Consumer' ? tx.establishment : tx.buyer}</td>
          <td class="p-2.5 text-[#4F695D]">${tx.date}</td>
          <td class="p-2.5">
            <span class="font-semibold text-[#12372C] block">${tx.total_value || tx.amount}</span>
            ${tx.deposit_paid && tx.deposit_paid !== '₱0.00' ? `<span class="text-[10px] text-[#3A826D] block">20% Dep: ${tx.deposit_paid}</span>` : ''}
          </td>
          <td class="p-2.5">${statusBadgeHtml(tx.status)}</td>
          <td class="p-2.5">
            <button type="button" onclick="openDigitalReceiptModal('${tx.id}')" class="bg-[#3A826D] hover:bg-[#1B4D3E] text-white font-semibold text-[10px] px-3 py-1 rounded-lg transition-colors cursor-pointer">
              Receipt
            </button>
          </td>
        </tr>
      `;
    });

    window.renderProgressiveRows(
      targetTbodyId,
      rowStrings,
      `<tr><td colspan="${colCount + 1}" class="p-4 text-center text-[#4F695D] text-xs">No transaction records found.</td></tr>`,
      colCount + 1,
      10
    );
  } catch (e) {}
}

// =========================================================
// 7. AUTH (FULL-SCREEN LOADER), MODALS, FILTERS & SORTING
// =========================================================
function initAdminLogin() {
  const loginForm = document.getElementById('loginForm');
  if (!loginForm) return;

  loginForm.addEventListener('submit', async function(e) {
    e.preventDefault();
    const email = document.getElementById('email').value;
    const password = document.getElementById('pw').value;
    const errorEl = document.getElementById('formError');
    const fullPageLoader = document.getElementById('loginFullPageLoader');

    errorEl.classList.add('hidden');
    if (fullPageLoader) {
      fullPageLoader.classList.remove('hidden');
      fullPageLoader.classList.add('flex');
    }

    try {
      const response = await fetch('/api/accounts/login/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_email: email, user_pass: password })
      });

      if (response.ok) {
        const data = await response.json();
        if (data.role !== 'Admin') {
          if (fullPageLoader) {
            fullPageLoader.classList.add('hidden');
            fullPageLoader.classList.remove('flex');
          }
          errorEl.textContent = 'Unauthorized: Administrator access required.';
          errorEl.classList.remove('hidden');
          return;
        }
        localStorage.setItem('admin_access_token', data.access);
        localStorage.setItem('admin_refresh_token', data.refresh);
        const titleEl = document.getElementById('loginLoaderTitle');
        if (titleEl) titleEl.textContent = 'Access Granted — Opening Console...';
        window.location.replace('/dashboard/');
      } else {
        if (fullPageLoader) {
          fullPageLoader.classList.add('hidden');
          fullPageLoader.classList.remove('flex');
        }
        const errData = await response.json().catch(() => ({}));
        errorEl.textContent = errData.error || 'Invalid email or password.';
        errorEl.classList.remove('hidden');
      }
    } catch (error) {
      if (fullPageLoader) {
        fullPageLoader.classList.add('hidden');
        fullPageLoader.classList.remove('flex');
      }
      errorEl.textContent = 'Network error. Please check your connection.';
      errorEl.classList.remove('hidden');
    }
  });
}

window.openModal = function(modalId) {
  const modal = document.getElementById(modalId);
  if (!modal) return;
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  document.body.classList.add('overflow-hidden');
  if (window.lucide) window.lucide.createIcons();
};

window.closeModal = function(modalId) {
  const modal = document.getElementById(modalId);
  if (!modal) return;
  modal.classList.add('hidden');
  modal.classList.remove('flex');
  document.body.classList.remove('overflow-hidden');
};

window.toggleModal = function(modalId) {
  const modal = document.getElementById(modalId);
  if (!modal) return;
  if (modal.classList.contains('hidden')) {
    window.openModal(modalId);
  } else {
    window.closeModal(modalId);
  }
};

window.toggleSection = function(sectionId) {
  const el = document.getElementById(sectionId);
  if (!el) return;
  el.classList.toggle('hidden');
};

function initCalendarPickers() {
  if (window.flatpickr) {
    flatpickr('.date-range-picker', {
      mode: 'range',
      dateFormat: 'Y-m-d',
      altInput: true,
      altFormat: 'F j, Y',
      conjunction: ' to ',
      onChange: function(selectedDates) {
        if (selectedDates.length === 2) {
          filterByCalendarRange(selectedDates[0], selectedDates[1]);
          setTimeout(() => window.triggerAllTablePaginations(true), 30);
        }
      }
    });
  }
}

window.filterByCalendarRange = function(startDate, endDate) {
  const visibleTables = document.querySelectorAll('table.user-role-table:not(.hidden)');
  if (!visibleTables.length) return;
  endDate.setHours(23, 59, 59);

  visibleTables.forEach(visibleTable => {
    const rows = visibleTable.querySelectorAll('tbody tr:not(.empty-row)');
    rows.forEach(row => {
      const dateStr = row.getAttribute('data-date');
      if (dateStr) {
        const rowDate = new Date(dateStr);
        row.style.display = (rowDate >= startDate && rowDate <= endDate) ? '' : 'none';
      }
    });
  });
};

window.clearCalendarFilter = function() {
  const pickerInput = document.querySelector('.date-range-picker');
  if (pickerInput && pickerInput._flatpickr) {
    pickerInput._flatpickr.clear();
  }
  const visibleTables = document.querySelectorAll('table.user-role-table:not(.hidden)');
  visibleTables.forEach(visibleTable => {
    const rows = visibleTable.querySelectorAll('tbody tr:not(.empty-row)');
    rows.forEach(row => {
      row.style.display = '';
    });
  });
  setTimeout(() => window.triggerAllTablePaginations(true), 30);
};

window.triggerSortByCriteria = function(selectElement) {
  const criteria = selectElement.value;
  const visibleTables = document.querySelectorAll('table.user-role-table:not(.hidden)');
  if (!visibleTables.length || !criteria) return;

  const isAscending = !criteria.includes('desc');

  visibleTables.forEach(visibleTable => {
    const tbody = visibleTable.querySelector('tbody');
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll('tr:not(.empty-row)'));

    rows.sort((a, b) => {
      if (criteria.includes('date')) {
        const dateA = new Date(a.getAttribute('data-date') || 0);
        const dateB = new Date(b.getAttribute('data-date') || 0);
        return isAscending ? dateA - dateB : dateB - dateA;
      }
      if (criteria.includes('status')) {
        const statA = (a.getAttribute('data-status') || '').toLowerCase();
        const statB = (b.getAttribute('data-status') || '').toLowerCase();
        return isAscending ? statA.localeCompare(statB) : statB.localeCompare(statA);
      }
      if (criteria.includes('tier')) {
        const tA = parseInt(a.getAttribute('data-tier') || 0);
        const tB = parseInt(b.getAttribute('data-tier') || 0);
        return isAscending ? tA - tB : tB - tA;
      }
      const nameA = a.children[1]?.textContent.trim().toLowerCase() || '';
      const nameB = b.children[1]?.textContent.trim().toLowerCase() || '';
      return isAscending ? nameA.localeCompare(nameB) : nameB.localeCompare(nameA);
    });

    rows.forEach(row => tbody.appendChild(row));
  });
  setTimeout(() => window.triggerAllTablePaginations(true), 30);
};

window.filterAndSortAnnouncements = function() {
  const listEl = document.getElementById('announcementHistoryList');
  if (!listEl) return;

  const query = (document.getElementById('announcementSearchInput')?.value || '').toLowerCase().trim();
  const sortCriteria = document.getElementById('announcementSortSelect')?.value || 'id-desc';
  const cards = Array.from(listEl.querySelectorAll('.broadcast-card-item'));

  cards.forEach(card => {
    const searchStr = (card.getAttribute('data-search') || card.textContent || '').toLowerCase();
    card.style.display = (!query || searchStr.includes(query)) ? '' : 'none';
  });

  cards.sort((a, b) => {
    if (sortCriteria === 'id-desc' || sortCriteria === 'id-asc') {
      const idA = parseInt(a.getAttribute('data-id') || '0', 10);
      const idB = parseInt(b.getAttribute('data-id') || '0', 10);
      return sortCriteria === 'id-asc' ? idA - idB : idB - idA;
    }
    if (sortCriteria === 'title-asc' || sortCriteria === 'title-desc') {
      const tA = (a.getAttribute('data-title') || '').toLowerCase();
      const tB = (b.getAttribute('data-title') || '').toLowerCase();
      return sortCriteria === 'title-asc' ? tA.localeCompare(tB) : tB.localeCompare(tA);
    }
    if (sortCriteria === 'audience-asc') {
      const aA = (a.getAttribute('data-audience') || '').toLowerCase();
      const aB = (b.getAttribute('data-audience') || '').toLowerCase();
      return aA.localeCompare(aB);
    }
    return 0;
  });

  cards.forEach(card => listEl.appendChild(card));
};

function bindSearchInputs() {
  const searchInputs = document.querySelectorAll('input[placeholder*="Search"]');
  searchInputs.forEach(input => {
    if (input.id === 'announcementSearchInput') return;
    input.removeEventListener('input', handleLiveSearch);
    input.addEventListener('input', handleLiveSearch);
  });
}

function setupGlobalListeners() {
  window.addEventListener('click', (event) => {
    if (event.target.classList.contains('modal-overlay')) {
      event.target.classList.add('hidden');
      event.target.classList.remove('flex');
      document.body.classList.remove('overflow-hidden');
    }
  });
  bindSearchInputs();
}

function handleLiveSearch(event) {
  const query = event.target.value.toLowerCase();
  const visibleTables = document.querySelectorAll('table.user-role-table:not(.hidden)');
  visibleTables.forEach(visibleTable => {
    const rows = visibleTable.querySelectorAll('tbody tr:not(.empty-row)');
    rows.forEach(row => {
      const searchData = (row.getAttribute('data-search') || row.textContent || '').toLowerCase();
      row.style.display = searchData.includes(query) ? '' : 'none';
    });
  });
  setTimeout(() => window.triggerAllTablePaginations(true), 20);
}

window.switchRoleTab = function(roleName, element) {
  sessionStorage.setItem('reserve_admin_user_role_tab', roleName);
  const tabs = document.querySelectorAll('.role-tab-btn');
  tabs.forEach(tab => {
    tab.classList.remove('bg-white', 'text-[#1B4D3E]', 'shadow-sm', 'border-b-2', 'border-[#1B4D3E]');
    tab.classList.add('text-gray-400', 'hover:text-[#1B4D3E]');
  });
  element.classList.remove('text-gray-400', 'hover:text-[#1B4D3E]');
  element.classList.add('bg-white', 'text-[#1B4D3E]', 'shadow-sm', 'border-b-2', 'border-[#1B4D3E]');

  const tables = document.querySelectorAll('.user-role-table');
  tables.forEach(table => table.classList.add('hidden'));

  const targetId = `table-${roleName.toLowerCase().replace(/\s+/g, '')}`;
  const activeTable = document.getElementById(targetId);
  if (activeTable) {
    activeTable.classList.remove('hidden');
    updateRoleStats(roleName);
  }
  setTimeout(() => {
    if (typeof window.applyUserTablePagination === 'function') {
      window.applyUserTablePagination(1);
    }
  }, 20);
};

function updateRoleStats(roleName) {
  const tableId = `table-${roleName.toLowerCase().replace(/\s+/g, '')}`;
  const table = document.getElementById(tableId);
  if (!table) return;

  const rows = table.querySelectorAll('tbody tr:not(.empty-row)');
  let total = rows.length;
  let active = 0;
  let suspendedOrPending = 0;

  rows.forEach(row => {
    const status = (row.getAttribute('data-status') || '').toLowerCase();
    if (status === 'active') {
      active++;
    } else {
      suspendedOrPending++;
    }
  });

  const statCards = document.querySelectorAll('main .grid-cols-1.sm\\:grid-cols-3 > div');
  if (statCards.length >= 3) {
    statCards[0].querySelector('h3').textContent = total;
    statCards[1].querySelector('h3').textContent = active;
    statCards[2].querySelector('h3').textContent = suspendedOrPending;

    statCards[0].querySelector('p').textContent = `Total ${roleName}s`;
    statCards[1].querySelector('p').textContent = `Active ${roleName}s`;
    statCards[2].querySelector('p').textContent = `Pending / Flagged`;
  }
}

// =========================================================
// 8. CONSUMER, MERCHANT & FOOD BANK DOSSIER MODALS
// =========================================================
window.openConsumerModal = function(btn) {
  const rawId = btn.dataset.rawId || (btn.dataset.id || '').replace(/\D/g, '');
  document.getElementById('activeConsumerRawId').value = rawId;
  document.getElementById('consViewName').textContent = btn.dataset.name || 'Consumer';
  document.getElementById('consViewFullName').textContent = btn.dataset.name || 'Consumer';
  document.getElementById('consViewId').textContent = btn.dataset.id || 'ID';
  document.getElementById('consViewEmail').textContent = btn.dataset.email || 'N/A';
  document.getElementById('consViewPhone').textContent = btn.dataset.phone || 'Not Provided';
  document.getElementById('consViewAddress').textContent = btn.dataset.address || 'Not Provided';
  document.getElementById('consViewDate').textContent = btn.dataset.date || 'N/A';

  const statusEl = document.getElementById('consStatusBadge');
  const statusStr = (btn.dataset.status || 'Active').toUpperCase();
  if (statusEl) {
    statusEl.textContent = statusStr;
    statusEl.className = statusStr === 'SUSPENDED'
      ? 'bg-[#FBE4E1] text-[#B3261E] px-2.5 py-0.5 rounded-lg text-[10px] font-semibold uppercase'
      : 'bg-[#E4F0E8] text-[#1F6B52] px-2.5 py-0.5 rounded-lg text-[10px] font-semibold uppercase';
  }

  if (btn.dataset.name) {
    document.getElementById('consInitials').textContent = btn.dataset.name.charAt(0).toUpperCase();
  }

  const firstTabBtn = document.querySelector('.cons-dossier-tab');
  if (firstTabBtn) window.switchDossierTab('cons', 'receipts', firstTabBtn);

  if (rawId) {
    loadAdminUserDossier('Consumer', rawId);
  }

  window.openModal('consumerViewModal');
};

window.openPendingModal = function(btn) {
  if (typeof window.openAutoScanKybModal === 'function') {
    window.openAutoScanKybModal(btn);
    return;
  }
  window.openModal('merchantPendingModal');
};

window.openPendingMerchantKybModal = window.openPendingModal;

window.openActiveModal = function(btn) {
  const merchantId = btn.dataset.id || (btn.getAttribute('data-id') || '');
  const busName = btn.dataset.name || 'Merchant';
  document.getElementById('activeMerchantId').value = merchantId;
  document.getElementById('activeModalTitle').textContent = busName;
  document.getElementById('activeBusName').textContent = btn.dataset.name || 'N/A';
  document.getElementById('activeMerchType').textContent = btn.dataset.type || 'N/A';
  document.getElementById('activeBusAddress').textContent = btn.dataset.address || 'Not Provided';
  document.getElementById('activeContactPerson').textContent = btn.dataset.contact || 'Not Provided';
  document.getElementById('activeContactPhone').textContent = btn.dataset.phone || 'Not Provided';
  document.getElementById('activeHotline').textContent = btn.dataset.hotline || 'N/A';
  document.getElementById('activeBusEmail').textContent = btn.dataset.email || 'N/A';
  document.getElementById('activeBusDate').textContent = btn.dataset.date || 'N/A';
  document.getElementById('activeStatusDate').textContent = btn.dataset.date || 'N/A';

  const firstMerchTab = document.querySelector('.merch-dossier-tab');
  if (firstMerchTab) window.switchDossierTab('merch', 'receipts', firstMerchTab);

  const txBody = document.getElementById('merchantTransactionsBody');
  if (txBody) {
    txBody.innerHTML = `<tr><td colspan="7" class="p-4 text-center text-[#4F695D]">Loading merchant transactions...</td></tr>`;
  }

  if (merchantId) {
    loadAdminUserDossier('Merchant', merchantId);
  }

  let ocrData = {};
  try {
    ocrData = JSON.parse(btn.dataset.ocr || '{}') || {};
  } catch (e) {
    ocrData = {};
  }

  if (document.getElementById('activePermitNo')) {
    document.getElementById('activePermitNo').textContent = ocrData.permit_number || 'Verified on File';
  }
  if (document.getElementById('activePermitTin')) {
    document.getElementById('activePermitTin').textContent = ocrData.tin_number || 'N/A';
  }
  if (document.getElementById('activePermitYear')) {
    document.getElementById('activePermitYear').textContent = ocrData.calendar_year || '2026-2027';
  }
  if (document.getElementById('activePermitNature')) {
    document.getElementById('activePermitNature').textContent = ocrData.nature_of_business || btn.dataset.type || 'Food Establishment';
  }
  if (document.getElementById('activePermitLGU')) {
    document.getElementById('activePermitLGU').textContent = ocrData.lgu_anchor || 'Cebu City LGU';
  }

  const expiryStr = btn.dataset.expiry || '';
  document.getElementById('activePermitExpiry').textContent = expiryStr || 'N/A';
  const expiryAlert = document.getElementById('activePermitExpiryAlert');
  const expiryAlertText = document.getElementById('activePermitExpiryAlertText');

  if (expiryStr && expiryAlert) {
    const today = new Date();
    const expiryDate = new Date(expiryStr);
    const diffDays = Math.ceil((expiryDate - today) / (1000 * 60 * 60 * 24));
    if (diffDays < 0) {
      expiryAlert.classList.remove('hidden');
      expiryAlertText.textContent = `Business Permit EXPIRED on ${expiryStr} (${Math.abs(diffDays)} days ago). Request renewal immediately.`;
    } else if (diffDays <= 30) {
      expiryAlert.classList.remove('hidden');
      expiryAlertText.textContent = `Business Permit expires in ${diffDays} days (${expiryStr}). Notify merchant to submit renewed permit.`;
    } else {
      expiryAlert.classList.add('hidden');
    }
  }

  const statusBadge = document.getElementById('activeStatusBadge');
  const statusWrapper = document.getElementById('activeStatusWrapper');
  const userStatus = (btn.dataset.status || 'Active').toLowerCase();

  if (userStatus === 'active') {
    statusBadge.innerHTML = `<i data-lucide="badge-check" class="w-3.5 h-3.5"></i> VERIFIED`;
    statusBadge.className = 'bg-[#3A826D] text-white px-3 py-1 rounded-lg text-xs font-semibold uppercase flex items-center gap-1';
    statusWrapper.className = 'bg-[#E8EFEA] px-4 py-2.5 rounded-xl flex items-center gap-3 w-full md:w-auto border border-[#DCE5DF]';
  } else {
    statusBadge.innerHTML = `<i data-lucide="alert-triangle" class="w-3.5 h-3.5"></i> ${btn.dataset.status}`;
    statusBadge.className = 'bg-[#B3261E] text-white px-3 py-1 rounded-lg text-xs font-semibold uppercase flex items-center gap-1';
    statusWrapper.className = 'bg-[#FBE4E1] px-4 py-2.5 rounded-xl flex items-center gap-3 w-full md:w-auto border border-[#B3261E]/30';
  }

  const rawPermitPath = ocrData.permit_data_url || ocrData.permit_url || btn.dataset.permit || '';
  const fileBtn = document.getElementById('activePermitBtn');
  if (fileBtn) {
    fileBtn.onclick = () => window.openVerifiedPermitDocument(merchantId, rawPermitPath, `${busName} — Verified Business Permit`);
  }

  const visitBtn = document.getElementById('activeVisitProfileBtn');
  if (visitBtn) {
    visitBtn.onclick = () => {
      window.closeModal('merchantActiveModal');
      window.navigateAdminSpa(`/food-listing/?merchant=MID-${merchantId}&open_profile=1`);
    };
  }

  window.openModal('merchantActiveModal');
};

window.openFoodBankPendingModal = function(btn) {
  const fbId = btn.dataset.id;
  const orgName = btn.dataset.name || 'Food Bank';
  document.getElementById('pendingFoodBankId').value = fbId;
  document.getElementById('fbPendingModalTitle').textContent = orgName;
  document.getElementById('fbPendingName').textContent = btn.dataset.name || 'N/A';
  document.getElementById('fbPendingAddress').textContent = btn.dataset.address || 'Not Provided';
  document.getElementById('fbPendingContact').textContent = btn.dataset.contact || 'Not Provided';
  document.getElementById('fbPendingPhone').textContent = btn.dataset.phone || 'Not Provided';
  document.getElementById('fbPendingHotline').textContent = btn.dataset.hotline || 'N/A';
  document.getElementById('fbPendingEmail').textContent = btn.dataset.email || 'N/A';
  document.getElementById('fbPendingDate').textContent = btn.dataset.date || 'N/A';
  document.getElementById('fbPendingExpiry').textContent = btn.dataset.expiry || 'N/A';

  const fileBtn = document.getElementById('fbPendingPermitBtn');
  if (fileBtn) {
    fileBtn.onclick = () => window.openVerifiedPermitDocument('', btn.dataset.permit, `${orgName} — Accreditation Certificate`);
  }

  window.openModal('foodBankPendingModal');
};

window.openFoodBankModal = function(btn) {
  const fbId = btn.dataset.id;
  const orgName = btn.dataset.name || 'Organization';
  document.getElementById('activeFoodBankId').value = fbId;
  document.getElementById('fbViewName').textContent = orgName;
  document.getElementById('fbViewAddress').textContent = btn.dataset.address || 'Not Provided';
  document.getElementById('fbViewContact').textContent = btn.dataset.contact || 'Not Provided';
  document.getElementById('fbViewPhone').textContent = btn.dataset.phone || 'Not Provided';
  document.getElementById('fbViewHotline').textContent = btn.dataset.hotline || 'N/A';
  document.getElementById('fbViewEmail').textContent = btn.dataset.email || 'N/A';
  document.getElementById('fbViewDate').textContent = btn.dataset.date || 'N/A';
  document.getElementById('fbViewExpiry').textContent = btn.dataset.expiry || 'N/A';

  if (fbId) {
    loadAdminUserDossier('FoodBank', fbId);
  }

  const statusBadge = document.getElementById('fbStatusBadge');
  const userStatus = (btn.dataset.status || 'Active').toLowerCase();
  if (userStatus === 'active') {
    statusBadge.textContent = 'VERIFIED NGO';
    statusBadge.className = 'bg-[#3A826D] text-white px-3 py-1 rounded-lg text-xs font-semibold uppercase';
  } else {
    statusBadge.textContent = (btn.dataset.status || 'Status').toUpperCase();
    statusBadge.className = 'bg-[#B3261E] text-white px-3 py-1 rounded-lg text-xs font-semibold uppercase';
  }

  const fileBtn = document.getElementById('fbPermitBtn');
  if (fileBtn) {
    fileBtn.onclick = () => window.openVerifiedPermitDocument('', btn.dataset.permit, `${orgName} — Accreditation Certificate`);
  }

  const visitBtn = document.getElementById('fbVisitProfileBtn');
  if (visitBtn) {
    visitBtn.onclick = () => {
      window.closeModal('foodBankViewModal');
      window.navigateAdminSpa(`/donations/?foodbank=FB-${fbId}&open_profile=1`);
    };
  }

  window.openModal('foodBankViewModal');
};

// =========================================================
// 9. ZERO-RELOAD COMPLIANCE API ACTIONS (USERS PAGE)
// =========================================================
function getCSRFToken() {
  let cookieValue = null;
  if (document.cookie && document.cookie !== '') {
    const cookies = document.cookie.split(';');
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, 10) === ('csrftoken=')) {
        cookieValue = decodeURIComponent(cookie.substring(10));
        break;
      }
    }
  }
  return cookieValue;
}

window.themeAlert = function(title, message, isSuccess = true) {
  return new Promise((resolve) => {
    document.getElementById('themeAlertTitle').textContent = title;
    document.getElementById('themeAlertMessage').textContent = message;

    const iconWrap = document.getElementById('themeAlertIconWrap');
    const icon = document.getElementById('themeAlertIcon');

    if (isSuccess) {
      iconWrap.className = 'w-10 h-10 rounded-full bg-[#E8EFEA] text-[#1B4D3E] flex items-center justify-center shrink-0';
      icon.setAttribute('data-lucide', 'check-circle');
    } else {
      iconWrap.className = 'w-10 h-10 rounded-full bg-[#FBE4E1] text-[#B3261E] flex items-center justify-center shrink-0';
      icon.setAttribute('data-lucide', 'alert-triangle');
    }
    if (window.lucide) window.lucide.createIcons();

    const modal = document.getElementById('themeAlertModal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');

    document.getElementById('themeAlertBtn').onclick = () => {
      modal.classList.add('hidden');
      modal.classList.remove('flex');
      resolve();
    };
  });
};

window.themeConfirm = function(title, message) {
  return new Promise((resolve) => {
    document.getElementById('themeConfirmTitle').textContent = title;
    document.getElementById('themeConfirmMessage').textContent = message;

    const modal = document.getElementById('themeConfirmModal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');

    document.getElementById('themeConfirmCancel').onclick = () => {
      modal.classList.add('hidden');
      modal.classList.remove('flex');
      resolve(false);
    };

    document.getElementById('themeConfirmOk').onclick = () => {
      modal.classList.add('hidden');
      modal.classList.remove('flex');
      resolve(true);
    };
  });
};

function patchUserRowStatusInPlace(role, accountId, newStatusLabel) {
  const rowId = `user-row-${role.replace(/\s+/g, '')}-${accountId}`;
  const row = document.getElementById(rowId);
  if (!row) return;

  row.setAttribute('data-status', newStatusLabel.toLowerCase());
  const statusCell = row.querySelector('.status-cell');
  if (statusCell) {
    statusCell.innerHTML = statusBadgeHtml(newStatusLabel);
  }

  const btn = row.querySelector('button');
  if (btn) {
    btn.dataset.status = newStatusLabel;
    if (newStatusLabel === 'Active') {
      btn.className = 'flex items-center gap-1.5 bg-[#3A826D] hover:bg-[#1B4D3E] text-white px-3.5 py-2 rounded-xl text-[11px] font-semibold transition-colors cursor-pointer';
      btn.innerHTML = `<i data-lucide="eye" class="w-3.5 h-3.5"></i> View`;
      if (role === 'Merchant') {
        btn.setAttribute('onclick', 'window.handleOpenActiveMerchantModal ? window.handleOpenActiveMerchantModal(this) : openActiveModal(this)');
      } else if (role === 'FoodBank') {
        btn.setAttribute('onclick', 'window.handleOpenFoodBankModal ? window.handleOpenFoodBankModal(this) : openFoodBankModal(this)');
      }
      if (window.lucide) window.lucide.createIcons();
    }
  }

  const currentTabRole = sessionStorage.getItem('reserve_admin_user_role_tab') || (role === 'FoodBank' ? 'Food Bank' : role);
  updateRoleStats(currentTabRole);
}

window.executeApprove = async function(role = 'Merchant') {
  const accountId = role === 'FoodBank'
    ? document.getElementById('pendingFoodBankId').value
    : document.getElementById('pendingMerchantId').value;

  const isConfirmed = await themeConfirm(
    'Approve Registration',
    `Are you sure you want to officially approve ${role} ID ${accountId} and activate their account?`
  );
  if (!isConfirmed) return;

  const payload = { action: 'approve', role: role };

  window.showAdminLoader(`Approving ${role}...`);
  try {
    const response = await fetch(`/api/accounts/admin/merchant/${accountId}/approval/`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify(payload)
    });
    window.hideAdminLoader();

    if (response.ok) {
      window.closeModal(role === 'FoodBank' ? 'foodBankPendingModal' : 'merchantPendingModal');
      patchUserRowStatusInPlace(role, accountId, 'Active');
      await themeAlert('Registration Approved!', `${role} account ${accountId} is now Active and verified.`, true);
    } else {
      await themeAlert('Approval Failed', 'The server rejected the approval.', false);
    }
  } catch (error) {
    window.hideAdminLoader();
    await themeAlert('Network Error', 'Could not reach the server. Please check your connection.', false);
  }
};

window.executeReject = async function(role = 'Merchant') {
  const accountId = role === 'FoodBank'
    ? document.getElementById('pendingFoodBankId').value
    : document.getElementById('pendingMerchantId').value;

  const reasonSelect = document.getElementById(role === 'FoodBank' ? 'fbRejectReasonSelect' : 'rejectReasonSelect');
  const reason = reasonSelect.value;

  if (!reason) {
    await themeAlert('Missing Information', 'Please select a specific reason for rejection from the dropdown menu before proceeding.', false);
    return;
  }

  const isConfirmed = await themeConfirm(
    'Reject & Request Resubmission',
    `Reject ${role} ID ${accountId}? An email will be sent instructing them to resubmit their document due to: "${reason}".`
  );
  if (!isConfirmed) return;

  window.showAdminLoader(`Updating ${role} status...`);
  try {
    const response = await fetch(`/api/accounts/admin/merchant/${accountId}/approval/`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({ action: 'reject', role: role, reason: reason })
    });
    window.hideAdminLoader();

    if (response.ok) {
      window.closeModal(role === 'FoodBank' ? 'foodBankPendingModal' : 'merchantPendingModal');
      patchUserRowStatusInPlace(role, accountId, 'Resubmit Required');
      await themeAlert('Resubmission Requested', 'The applicant has been emailed with a link to upload a new document.', true);
    } else {
      await themeAlert('Rejection Failed', 'The server failed to update the status.', false);
    }
  } catch (error) {
    window.hideAdminLoader();
    await themeAlert('Network Error', 'Could not reach the server.', false);
  }
};

window.executeConsumerSuspend = async function() {
  const consId = document.getElementById('activeConsumerRawId').value;
  const reason = document.getElementById('consumerSuspendReason').value;

  if (!reason) {
    await themeAlert('Select Reason', 'Please select a suspension reason from the dropdown first.', false);
    return;
  }

  const isConfirmed = await themeConfirm(
    'Suspend Consumer Account',
    `Suspend Consumer CID-${consId} for "${reason}"? They will be notified via email with an appeal link.`
  );
  if (!isConfirmed) return;

  window.showAdminLoader('Suspending Consumer account...');
  try {
    const response = await fetch(`/api/accounts/admin/merchant/${consId}/approval/`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({ action: 'suspend', role: 'Consumer', reason: reason })
    });
    window.hideAdminLoader();

    if (response.ok) {
      patchUserRowStatusInPlace('Consumer', consId, 'Suspended');
      const statusEl = document.getElementById('consStatusBadge');
      if (statusEl) {
        statusEl.textContent = 'SUSPENDED';
        statusEl.className = 'bg-[#FBE4E1] text-[#B3261E] px-2.5 py-0.5 rounded-lg text-[10px] font-semibold uppercase';
      }
      await themeAlert('Consumer Suspended', 'Account suspended in-place and appeal link dispatched to their email.', true);
    } else {
      await themeAlert('Action Failed', 'Could not suspend the consumer account.', false);
    }
  } catch (error) {
    window.hideAdminLoader();
    await themeAlert('Network Error', 'Could not reach the server.', false);
  }
};

window.executeSuspend = async function(role = 'Merchant') {
  const accountId = role === 'FoodBank'
    ? document.getElementById('activeFoodBankId').value
    : document.getElementById('activeMerchantId').value;

  const reasonSelect = document.getElementById(role === 'FoodBank' ? 'fbSuspendReason' : 'merchantSuspendReason');
  const reason = reasonSelect.value;

  if (!reason) {
    await themeAlert('Select Reason', 'Please select a suspension reason from the dropdown before suspending.', false);
    return;
  }

  const isConfirmed = await themeConfirm(
    `Suspend ${role} Account`,
    `Suspend ${role} ID ${accountId} due to "${reason}"? They will receive an email notification with an appeal link.`
  );
  if (!isConfirmed) return;

  window.showAdminLoader(`Suspending ${role} account...`);
  try {
    const response = await fetch(`/api/accounts/admin/merchant/${accountId}/approval/`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
      body: JSON.stringify({ action: 'suspend', role: role, reason: reason })
    });
    window.hideAdminLoader();

    if (response.ok) {
      patchUserRowStatusInPlace(role, accountId, 'Suspended');
      if (role === 'FoodBank') {
        const b = document.getElementById('fbStatusBadge');
        if (b) {
          b.textContent = 'SUSPENDED';
          b.className = 'bg-[#B3261E] text-white px-3 py-1 rounded-lg text-xs font-semibold uppercase';
        }
      } else {
        const b = document.getElementById('activeStatusBadge');
        if (b) {
          b.textContent = 'SUSPENDED';
          b.className = 'bg-[#B3261E] text-white px-3 py-1 rounded-lg text-xs font-semibold uppercase flex items-center gap-1';
        }
      }
      await themeAlert('Account Suspended', `The ${role} has been suspended in-place and emailed an appeal link.`, true);
    } else {
      await themeAlert('Suspension Failed', 'The server could not process the suspension.', false);
    }
  } catch (error) {
    window.hideAdminLoader();
    await themeAlert('Network Error', 'Could not reach the server.', false);
  }
};

// =========================================================
// 10. ZERO-RELOAD FOOD LISTING, PAYOUT & ANNOUNCEMENT ACTIONS
// =========================================================
window.openFoodDetailModal = function(btn) {
  document.getElementById('currentFoodRawId').value = btn.dataset.rawId || '';
  document.getElementById('currentFoodRowId').value = `row-${btn.dataset.id}`;
  document.getElementById('foodModalTitle').textContent = btn.dataset.title || 'Food Item';
  document.getElementById('foodModalDesc').textContent = btn.dataset.desc || '';
  document.getElementById('foodModalQty').textContent = btn.dataset.qty || '1';
  document.getElementById('foodModalOrigPrice').textContent = btn.dataset.orig || '₱0.00';
  document.getElementById('foodModalDiscPrice').textContent = btn.dataset.disc || '₱0.00';
  if (document.getElementById('foodModalDate')) {
    document.getElementById('foodModalDate').textContent = btn.dataset.date || 'N/A';
  }
  document.getElementById('foodModalExpiry').textContent = btn.dataset.expiry || 'N/A';
  document.getElementById('foodModalMerchant').textContent = btn.dataset.merchant || 'Merchant';
  document.getElementById('foodModalAddress').textContent = btn.dataset.address || 'Cebu City';

  const imgEl = document.getElementById('foodModalImg');
  if (imgEl) {
    imgEl.src = btn.dataset.img || 'https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=400&q=80';
  }
  window.openModal('foodModal');
};

window.flagFoodListingModal = async function() {
  const rawId = document.getElementById('currentFoodRawId').value;
  const rowId = document.getElementById('currentFoodRowId').value;

  window.showAdminLoader('Flagging food listing...');
  const res = await fetch('/api/accounts/admin/portal-action/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
    body: JSON.stringify({ module: 'listing', action: 'flag', id: rawId })
  });
  window.hideAdminLoader();
  window.closeModal('foodModal');

  if (res.ok) {
    const row = document.getElementById(rowId);
    if (row) {
      row.setAttribute('data-bucket', 'flagged_expired');
      row.setAttribute('data-status', 'flagged');
      const statusCell = row.querySelector('.status-cell');
      if (statusCell) {
        statusCell.innerHTML = `<span class="bg-[#FBE4E1] text-[#B3261E] px-2.5 py-1 rounded-lg text-[10px] font-semibold">Flagged</span>`;
      }
    }
    const activeBadge = document.getElementById('activeListingsBadge');
    const flaggedBadge = document.getElementById('flaggedCountBadge');
    if (activeBadge) activeBadge.textContent = Math.max(0, (parseInt(activeBadge.textContent) || 1) - 1);
    if (flaggedBadge) flaggedBadge.textContent = (parseInt(flaggedBadge.textContent) || 0) + 1;
    await themeAlert('Listing Flagged', 'Food listing flagged in-place and merchant notified.', true);
  }
};

window.removeFoodListingModal = async function() {
  const rawId = document.getElementById('currentFoodRawId').value;
  const rowId = document.getElementById('currentFoodRowId').value;
  window.closeModal('foodModal');
  await window.removeFoodListing(rawId, rowId);
};

window.removeFoodListing = async function(rawId, rowId) {
  const isSure = await themeConfirm('Remove Food Listing', 'Are you sure you want to permanently remove this food listing from the database?');
  if (!isSure) return;

  window.showAdminLoader('Removing food listing...');
  await fetch('/api/accounts/admin/portal-action/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
    body: JSON.stringify({ module: 'listing', action: 'delete', id: rawId })
  });
  window.hideAdminLoader();

  const row = document.getElementById(rowId);
  if (row) row.remove();
  window.triggerAllTablePaginations(false);
  await themeAlert('Listing Removed', 'The food listing has been removed.', true);
};

window.openPayoutApprovalModal = function(btn) {
  if (typeof window.openPayoutReceiptModal === 'function') {
    window.openPayoutReceiptModal(btn);
    return;
  }
  window.openModal('payoutApprovalModal');
};

window.handleAnnouncementSubmit = async function(e) {
  e.preventDefault();
  const audience = document.getElementById('announcementAudience').value;
  const title = document.getElementById('announcementTitle').value;
  const body = document.getElementById('announcementBody').value;

  window.showAdminLoader('Publishing broadcast...');
  const res = await fetch('/api/accounts/admin/portal-action/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
    body: JSON.stringify({ module: 'announcement', action: 'create', audience, title, body })
  });
  const data = await res.json().catch(() => ({}));
  window.hideAdminLoader();

  if (res.ok) {
    const listEl = document.getElementById('announcementHistoryList');
    const emptyMsg = document.getElementById('emptyBroadcastMsg');
    if (emptyMsg) emptyMsg.remove();

    if (listEl) {
      const nowStr = new Date().toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
      const searchStr = `${data.id} ${title.toLowerCase()} ${body.toLowerCase()} ${audience.toLowerCase()}`;
      const cardHtml = `
        <div id="broadcast-card-${data.id}"
             data-id="${data.id}"
             data-title="${title.toLowerCase()}"
             data-audience="${audience.toLowerCase()}"
             data-search="${searchStr}"
             class="broadcast-card-item border border-[#DCE5DF] p-4 rounded-xl bg-[#F7F9F7] hover:bg-white flex justify-between items-start transition-all">
          <div class="pr-3">
            <span class="text-[10px] font-semibold uppercase bg-[#E8EFEA] text-[#1B4D3E] px-2.5 py-1 rounded-md border border-[#D5E3D9]">${audience}</span>
            <h5 class="font-semibold text-sm text-[#12372C] mt-2">${title}</h5>
            <p class="text-xs text-[#4F695D] mt-1 leading-relaxed">${body}</p>
            <p class="text-[10px] text-[#4F695D] font-medium mt-2.5">Broadcast ID #${data.id} • Published ${nowStr}</p>
          </div>
          <button onclick="deleteAnnouncement('${data.id}')" class="w-8 h-8 rounded-lg bg-white hover:bg-[#FBE4E1] border border-[#DCE5DF] text-[#4F695D] hover:text-[#B3261E] flex items-center justify-center shrink-0 transition-colors">
            <i data-lucide="trash-2" class="w-4 h-4"></i>
          </button>
        </div>
      `;
      listEl.insertAdjacentHTML('afterbegin', cardHtml);
      if (window.lucide) window.lucide.createIcons();
    }
    document.getElementById('announcementTitle').value = '';
    document.getElementById('announcementBody').value = '';
    await themeAlert('Broadcast Published', 'Your announcement has been published and added to the ledger!', true);
  } else {
    await themeAlert('Broadcast Failed', 'Failed to publish broadcast.', false);
  }
};

window.deleteAnnouncement = async function(id) {
  const isSure = await themeConfirm('Delete Broadcast', 'Are you sure you want to delete this broadcast from the database?');
  if (!isSure) return;

  window.showAdminLoader('Deleting broadcast...');
  const res = await fetch('/api/accounts/admin/portal-action/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
    body: JSON.stringify({ module: 'announcement', action: 'delete', id: id })
  });
  window.hideAdminLoader();

  if (res.ok) {
    const card = document.getElementById(`broadcast-card-${id}`);
    if (card) card.remove();
    await themeAlert('Deleted', 'Broadcast removed.', true);
  }
};

window.sendAdminPasswordResetEmail = async function() {
  const btn = document.getElementById('sendAdminResetLinkBtn');
  const feedback = document.getElementById('adminResetFeedback');
  if (!btn || !feedback) return;

  btn.disabled = true;
  btn.textContent = 'Sending Link...';
  feedback.classList.add('hidden');

  try {
    const res = await fetch('/api/accounts/forgot-password/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_admin: true })
    });
    const data = await res.json();
    feedback.classList.remove('hidden');
    if (res.ok) {
      feedback.className = 'p-3 rounded-xl font-semibold text-[11px] bg-[#E4F0E8] text-[#1F6B52] border border-emerald-200';
      feedback.textContent = data.message || 'Password reset link sent to reserve.org.com@gmail.com!';
      btn.textContent = 'Link Sent ✓';
    } else {
      feedback.className = 'p-3 rounded-xl font-semibold text-[11px] bg-[#FBE4E1] text-[#B3261E] border border-[#B3261E]/30';
      feedback.textContent = data.error || 'Could not send reset email.';
      btn.disabled = false;
      btn.textContent = 'Send Reset Link';
    }
  } catch (err) {
    feedback.classList.remove('hidden');
    feedback.className = 'p-3 rounded-xl font-semibold text-[11px] bg-[#FBE4E1] text-[#B3261E] border border-[#B3261E]/30';
    feedback.textContent = 'Network error while requesting password reset.';
    btn.disabled = false;
    btn.textContent = 'Send Reset Link';
  }
};

window.filterListingsByBucket = function(bucket, btnEl) {
  sessionStorage.setItem('reserve_admin_listing_bucket_tab', bucket);
  document.querySelectorAll('.listing-bucket-tab').forEach(t => {
    t.className = 'listing-bucket-tab px-5 py-2.5 text-xs font-bold rounded-2xl text-gray-500 hover:text-[#1B4D3E]';
  });
  btnEl.className = 'listing-bucket-tab px-5 py-2.5 text-xs font-black rounded-2xl bg-white text-[#1B4D3E] shadow-sm border-b-2 border-[#1B4D3E]';

  document.querySelectorAll('tr.listing-row').forEach(row => {
    if (bucket === 'all' || row.dataset.bucket === bucket) {
      row.style.display = '';
    } else {
      row.style.display = 'none';
    }
  });
};

window.toggleSeeAllMerchants = function() {
  const extraCards = document.querySelectorAll('.extra-merchant-card');
  const btn = document.getElementById('toggleAllMerchantsBtn');
  if (!extraCards.length || !btn) return;

  const isCurrentlyHidden = extraCards[0].classList.contains('hidden');
  extraCards.forEach(card => {
    if (isCurrentlyHidden) card.classList.remove('hidden');
    else card.classList.add('hidden');
  });
  btn.textContent = isCurrentlyHidden
    ? 'Show Less'
    : `See All (${document.querySelectorAll('.merchant-dir-card').length})`;
};

window.openMerchantDirectoryProfile = function(cardEl) {
  const merchId = cardEl.dataset.id;
  const name = cardEl.dataset.name || 'Merchant';
  document.getElementById('mpModalInitial').textContent = name.charAt(0).toUpperCase();
  document.getElementById('mpModalName').textContent = name;
  document.getElementById('mpModalRatingBadge').textContent = `★ ${cardEl.dataset.rating || '5.0'} (${cardEl.dataset.reviews || '0'} reviews)`;
  document.getElementById('mpModalSub').textContent = `${cardEl.dataset.idLabel} • ${cardEl.dataset.type} • ${cardEl.dataset.status}`;
  document.getElementById('mpModalContact').textContent = `Contact: ${cardEl.dataset.contact}`;
  document.getElementById('mpModalPhone').textContent = `Phone: ${cardEl.dataset.phone} | Hotline: ${cardEl.dataset.hotline}`;
  document.getElementById('mpModalEmail').textContent = cardEl.dataset.email;
  document.getElementById('mpModalAddress').textContent = cardEl.dataset.address;
  document.getElementById('mpModalWallet').textContent = cardEl.dataset.wallet;
  document.getElementById('mpModalPlan').textContent = cardEl.dataset.plan;
  document.getElementById('mpModalExpiry').textContent = cardEl.dataset.expiry;
  document.getElementById('mpModalActiveCount').textContent = cardEl.dataset.active || '0';
  document.getElementById('mpModalFlaggedCount').textContent = cardEl.dataset.flagged || '0';

  const filterBtn = document.getElementById('mpFilterPageBtn');
  if (filterBtn) {
    filterBtn.onclick = () => {
      window.closeModal('merchantDirectoryProfileModal');
      window.navigateAdminSpa(`/food-listing/?merchant=MID-${merchId}`);
    };
  }

  let items = [];
  try {
    items = JSON.parse(cardEl.dataset.items || '[]');
  } catch (e) {
    items = [];
  }

  const listingRows = items.map(it => {
    const photoUrl = it.image_url || 'https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=200&q=80';
    return `
      <tr class="hover:bg-[#F7F9F7]">
        <td class="p-2.5 font-mono font-semibold text-[#1B4D3E]">${it.id}</td>
        <td class="p-2.5">
          <div class="flex items-center gap-2.5">
            <img src="${photoUrl}" onerror="this.src='https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=200&q=80'" class="w-9 h-9 rounded-lg object-cover border border-[#DCE5DF] shrink-0">
            <span class="font-semibold text-[#12372C]">${it.title}</span>
          </div>
        </td>
        <td class="p-2.5"><span class="line-through text-[#4F695D] text-[10px] mr-1">${it.orig_price}</span><strong class="text-[#1B4D3E]">${it.disc_price}</strong></td>
        <td class="p-2.5 font-semibold text-[#12372C]">${it.quantity}</td>
        <td class="p-2.5 text-[#4F695D]">${it.expiry}</td>
        <td class="p-2.5">${statusBadgeHtml(it.status)}</td>
      </tr>
    `;
  });

  window.renderProgressiveRows(
    'mpModalListingsBody',
    listingRows,
    `<tr><td colspan="6" class="p-3 text-center text-[#4F695D]">No food listings posted by this merchant yet.</td></tr>`,
    6,
    10
  );

  loadLiveUserTransactions('Merchant', merchId, 'mpModalTransactionsBody', 6);
  window.openModal('merchantDirectoryProfileModal');
};

// =========================================================
// 11. ZERO-RELOAD ADMIN DONATION HUB CONTROLLERS
// =========================================================
window.filterDonationHubSection = function(section, btnEl) {
  sessionStorage.setItem('reserve_admin_donation_hub_tab', section);
  document.querySelectorAll('.don-hub-tab').forEach(t => {
    t.className = 'don-hub-tab px-5 py-2.5 text-xs font-bold rounded-2xl text-gray-500 hover:text-[#1B4D3E]';
  });
  btnEl.className = 'don-hub-tab px-5 py-2.5 text-xs font-black rounded-2xl bg-white text-[#1B4D3E] shadow-sm border-b-2 border-[#1B4D3E]';

  const secAppeals = document.getElementById('section-appeals');
  const secDonations = document.getElementById('section-donations');
  if (!secAppeals || !secDonations) return;

  if (section === 'all') {
    secAppeals.style.display = '';
    secDonations.style.display = '';
  } else if (section === 'appeals') {
    secAppeals.style.display = '';
    secDonations.style.display = 'none';
  } else {
    secAppeals.style.display = 'none';
    secDonations.style.display = '';
  }
};

window.toggleSeeAllFoodBanks = function() {
  const extraCards = document.querySelectorAll('.extra-fb-card');
  const btn = document.getElementById('toggleAllFoodBanksBtn');
  if (!extraCards.length || !btn) return;

  const isHidden = extraCards[0].classList.contains('hidden');
  extraCards.forEach(c => {
    if (isHidden) c.classList.remove('hidden');
    else c.classList.add('hidden');
  });
  btn.textContent = isHidden
    ? 'Show Less'
    : `See All (${document.querySelectorAll('.fb-dir-card').length})`;
};

window.openAdminAppealModal = function(btn) {
  const rawId = btn.dataset.rawId;
  const batchCode = btn.dataset.batch;

  document.getElementById('apModalBatch').textContent = `Appeal ${batchCode} — ${btn.dataset.fb}`;
  document.getElementById('apModalSub').textContent = `Status: ${btn.dataset.status} • Posted ${btn.dataset.date}`;
  document.getElementById('apModalFbName').textContent = btn.dataset.fb;
  document.getElementById('apModalFbContact').textContent = btn.dataset.fbContact;
  document.getElementById('apModalFbEmail').textContent = btn.dataset.fbEmail;
  document.getElementById('apModalFbAddr').textContent = btn.dataset.fbAddr;

  document.getElementById('apModalMerchName').textContent = btn.dataset.merchant;
  document.getElementById('apModalMerchContact').textContent = btn.dataset.merchantContact;
  document.getElementById('apModalMerchAddr').textContent = btn.dataset.merchantAddr;
  document.getElementById('apModalExpiry').textContent = btn.dataset.expiry;
  document.getElementById('apModalReason').textContent = btn.dataset.reason;
  document.getElementById('apModalInfo').textContent = btn.dataset.info;

  let items = [];
  try {
    items = JSON.parse(btn.dataset.items || '[]');
  } catch (e) {
    items = [];
  }

  const container = document.getElementById('apModalItemsContainer');
  if (container) {
    container.innerHTML = items.map(it => `
      <div class="flex items-center justify-between bg-[#F7F9F7] p-2.5 rounded-xl border border-[#DCE5DF]">
        <div class="flex items-center gap-3">
          <img src="${it.image_url}" onerror="this.src='https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=200&q=80'" class="w-10 h-10 rounded-lg object-cover border border-[#DCE5DF]">
          <div>
            <span class="font-semibold text-[#12372C] block">${it.qty_needed} ${it.unit_name} — ${it.food_name}</span>
            <span class="text-[10px] text-[#B3261E] font-medium">Exp: ${it.expiry_display}</span>
          </div>
        </div>
        ${statusBadgeHtml(it.status)}
      </div>
    `).join('');
  }

  document.getElementById('apModalApproveBtn').onclick = () => {
    window.closeModal('adminAppealDetailModal');
    window.handleAdminDonationHubAction('appeal', 'approve', rawId, batchCode);
  };
  document.getElementById('apModalCompleteBtn').onclick = () => {
    window.closeModal('adminAppealDetailModal');
    window.handleAdminDonationHubAction('appeal', 'complete', rawId, batchCode);
  };
  document.getElementById('apModalRejectBtn').onclick = () => {
    window.closeModal('adminAppealDetailModal');
    window.handleAdminDonationHubAction('appeal', 'reject', rawId, batchCode);
  };

  window.openModal('adminAppealDetailModal');
};

window.openAdminDonationModal = function(btn) {
  const rawId = btn.dataset.rawId;
  document.getElementById('donModalTitle').textContent = `${btn.dataset.id} — ${btn.dataset.title}`;
  document.getElementById('donModalSub').textContent = `Status: ${btn.dataset.status} • Category: ${btn.dataset.type}`;
  document.getElementById('donModalFoodName').textContent = btn.dataset.title;
  document.getElementById('donModalDesc').textContent = btn.dataset.desc;
  document.getElementById('donModalQty').textContent = btn.dataset.qty;
  document.getElementById('donModalExpiry').textContent = btn.dataset.expiry;
  document.getElementById('donModalDate').textContent = btn.dataset.date;

  document.getElementById('donModalMerchant').textContent = btn.dataset.merchant;
  document.getElementById('donModalMerchantContact').textContent = btn.dataset.merchantContact;
  document.getElementById('donModalMerchantAddr').textContent = btn.dataset.merchantAddr;

  document.getElementById('donModalFb').textContent = btn.dataset.fb;
  document.getElementById('donModalFbContact').textContent = btn.dataset.fbContact;
  document.getElementById('donModalFbAddr').textContent = btn.dataset.fbAddr;

  const imgEl = document.getElementById('donModalImg');
  if (imgEl) {
    imgEl.src = btn.dataset.img || 'https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=400&q=80';
  }

  document.getElementById('donModalCompleteBtn').onclick = () => {
    window.closeModal('adminDonationDetailModal');
    window.handleAdminDonationHubAction('donation', 'complete', rawId, '');
  };
  document.getElementById('donModalFlagBtn').onclick = () => {
    window.closeModal('adminDonationDetailModal');
    window.handleAdminDonationHubAction('donation', 'flag', rawId, '');
  };
  document.getElementById('donModalCancelBtn').onclick = () => {
    window.closeModal('adminDonationDetailModal');
    window.handleAdminDonationHubAction('donation', 'cancel', rawId, '');
  };

  window.openModal('adminDonationDetailModal');
};

window.handleAdminDonationHubAction = async function(recordType, action, rawId, batchCode) {
  const actionLabels = {
    approve: 'Approve Appeal & Set Ongoing',
    complete: 'Confirm Handshake & Mark Successful',
    reject: 'Reject Appeal',
    flag: 'Flag Donation Record',
    cancel: 'Cancel Donation & Restore Stock',
  };
  const isSure = await themeConfirm(
    actionLabels[action] || 'Confirm Action',
    `Are you sure you want to perform "${actionLabels[action] || action}"?`
  );
  if (!isSure) return;

  window.showAdminLoader('Updating donation hub record...');
  const res = await fetch('/api/accounts/admin/portal-action/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
    body: JSON.stringify({
      module: 'donation',
      record_type: recordType,
      action: action,
      id: rawId,
      batch_code: batchCode,
    })
  });
  const data = await res.json().catch(() => ({}));
  window.hideAdminLoader();

  if (res.ok) {
    const newStatus = {
      approve: 'Ongoing',
      complete: 'Successful',
      reject: 'Rejected',
      flag: 'Flagged',
      cancel: 'Cancelled',
    }[action] || 'Updated';

    const rowId = recordType === 'appeal' ? `appeal-row-${batchCode}` : `don-row-DON-${rawId}`;
    const row = document.getElementById(rowId);
    if (row && row.children.length >= 7) {
      row.children[6].innerHTML = statusBadgeHtml(newStatus);
      const eyeBtn = row.querySelector('button');
      if (eyeBtn) eyeBtn.dataset.status = newStatus;
    }
    await themeAlert('Updated', data.message || 'Donation hub record updated in-place!', true);
  } else {
    await themeAlert('Action Failed', data.error || 'Could not update record.', false);
  }
};

window.openFoodBankDirectoryProfile = function(cardEl) {
  const orgId = cardEl.dataset.id;
  const name = cardEl.dataset.name || 'Food Bank';
  document.getElementById('fbDirInitial').textContent = name.charAt(0).toUpperCase();
  document.getElementById('fbDirName').textContent = name;
  document.getElementById('fbDirSub').textContent = `${cardEl.dataset.idLabel} • Status: ${cardEl.dataset.status}`;
  document.getElementById('fbDirContact').textContent = `Coordinator: ${cardEl.dataset.contact}`;
  document.getElementById('fbDirPhone').textContent = `Phone: ${cardEl.dataset.phone} | Hotline: ${cardEl.dataset.hotline}`;
  document.getElementById('fbDirEmail').textContent = cardEl.dataset.email;
  document.getElementById('fbDirAddress').textContent = cardEl.dataset.address;
  document.getElementById('fbDirReceived').textContent = cardEl.dataset.received || '0';
  document.getElementById('fbDirDate').textContent = cardEl.dataset.date || 'N/A';
  document.getElementById('fbDirActiveReq').textContent = cardEl.dataset.activeReq || '0';
  document.getElementById('fbDirClaimedDon').textContent = cardEl.dataset.claimedDon || '0';

  const permitBtn = document.getElementById('fbDirPermitBtn');
  if (permitBtn) {
    permitBtn.onclick = () => window.openVerifiedPermitDocument('', cardEl.dataset.permit, `${name} — Accreditation Certificate`);
  }

  const filterBtn = document.getElementById('fbDirFilterBtn');
  if (filterBtn) {
    filterBtn.onclick = () => {
      window.closeModal('foodBankDirectoryProfileModal');
      window.navigateAdminSpa(`/donations/?foodbank=FB-${orgId}`);
    };
  }

  let history = [];
  try {
    history = JSON.parse(cardEl.dataset.history || '[]');
  } catch (e) {
    history = [];
  }

  const historyRows = history.map(h => `
    <tr class="hover:bg-[#F7F9F7]">
      <td class="p-2.5 font-mono font-semibold text-[#1B4D3E]">${h.id}</td>
      <td class="p-2.5 text-[#4F695D] font-medium">${h.type}</td>
      <td class="p-2.5 font-semibold text-[#12372C]">${h.item}</td>
      <td class="p-2.5 text-[#12372C]">${h.merchant}</td>
      <td class="p-2.5 text-[#4F695D]">${h.date}</td>
      <td class="p-2.5">${statusBadgeHtml(h.status)}</td>
    </tr>
  `);

  window.renderProgressiveRows(
    'fbDirHistoryBody',
    historyRows,
    `<tr><td colspan="6" class="p-3 text-center text-[#4F695D]">No appeals or claimed donations recorded yet.</td></tr>`,
    6,
    10
  );

  window.openModal('foodBankDirectoryProfileModal');
};