let currentProfiles = [];
let currentAgents = [];
let editingAgentId = null;
let selectedFile = null;

const statTotalProfiles = document.getElementById("stat-total-profiles");
const statActiveUsers = document.getElementById("stat-active-users");
const profilesGrid = document.getElementById("profiles-grid");
const filterInput = document.getElementById("filter-input");
const logsTbody = document.getElementById("logs-tbody");
const btnOpenUpload = document.getElementById("btn-open-upload");
const btnRefresh = document.getElementById("btn-refresh");
const modalUpload = document.getElementById("modal-upload");
const btnCloseModal = document.getElementById("btn-close-modal");
const btnCancelModal = document.getElementById("btn-cancel-modal");
const formUpload = document.getElementById("form-upload");
const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const fileNameDisplay = document.getElementById("file-name-display");
const toastContainer = document.getElementById("toast-container");

function showToast(message, type = "info") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 3500);
}

function formatQuotaTime(value) {
  if (!value) return '未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false
  });
}

async function loadQuotaHistory(profileId) {
  const target = document.getElementById(`quota-history-${profileId}`);
  const day = document.getElementById(`quota-history-day-${profileId}`).value;
  target.textContent = '正在加载…';
  try {
    const response = await fetch(`/api/profiles/${profileId}/quota-history?day=${encodeURIComponent(day)}`);
    if (!response.ok) throw new Error('加载失败');
    const rows = await response.json();
    target.innerHTML = rows.length ? rows.map((row, index) => `<div class="quota-history-entry">
      <strong>查询：${escapeHtml(formatQuotaTime(row.observed_at))}</strong>
      ${['five_hour', 'weekly'].map(key => {
        const w = row.windows[key];
        if (!w || !w.available) return '';
        const previous = rows[index + 1];
        const old = previous?.windows[key];
        const decreased = old?.used_percent != null && w.used_percent != null && w.used_percent < old.used_percent;
        const resetChanged = old?.reset_at && w.reset_at && Math.abs(new Date(w.reset_at) - new Date(old.reset_at)) > 60000;
        return `<div>${escapeHtml(w.name)}：已用 ${w.used_percent == null ? '未知' : escapeHtml(String(w.used_percent)) + '%'}<br>
          接口记录的重置时间（仅供对照）：${escapeHtml(formatQuotaTime(w.reset_at))}${w.reset_source === 'estimated' ? '（按接口剩余秒数推算）' : ''}
          ${decreased ? `<div class="quota-change">${w.used_percent === 0 ? '实际恢复（用量归零）' : '用量下降'}：${escapeHtml(String(old.used_percent))}% → ${escapeHtml(String(w.used_percent))}%<br>发生区间：${escapeHtml(formatQuotaTime(previous.observed_at))} 至本次查询</div>` : ''}
          ${resetChanged ? `<div class="quota-change">预计重置时间已变更，上次：${escapeHtml(formatQuotaTime(old.reset_at))}</div>` : ''}</div>`;
      }).join('')}</div>`).join('') : '当天暂无记录；记录从新版服务启用后开始。';
  } catch (error) {
    target.textContent = '历史加载失败，请重试。';
  }
}

function formatPlanName(rawPlan) {
  if (!rawPlan) return 'Team 团队版';
  const p = rawPlan.toUpperCase().trim();
  if (p.includes('PROLITE') || p.includes('BUSINESS') || p.includes('TEAM')) return 'Team 团队版';
  if (p.includes('ENTERPRISE')) return 'Enterprise 企业版';
  if (p.includes('PLUS')) return 'Plus 会员';
  if (p.includes('PRO')) return 'Pro 专业版';
  if (p.includes('FREE')) return 'Free 免费版';
  return rawPlan;
}

async function fetchProfiles({background = false} = {}) {
  try {
    const res = await fetch("/api/profiles");
    if (!res.ok) throw new Error("加载配置失败");
    currentProfiles = await res.json();
    // 阅读历史、选择日期或复制文字时保持节点，避免轮询打断交互。
    const selection = window.getSelection();
    const interacting = profilesGrid.querySelector('.quota-history[open]') ||
      (profilesGrid.contains(document.activeElement) && document.activeElement.matches('input, select, textarea')) ||
      (selection && !selection.isCollapsed && profilesGrid.contains(selection.anchorNode));
    if (!background || !interacting) renderProfiles();
    updateStats();
  } catch (err) {
    console.error(err);
  }
}

async function fetchLogs() {
  try {
    const res = await fetch("/api/logs?limit=20");
    if (!res.ok) return;
    const logs = await res.json();
    renderLogs(logs);
  } catch (err) {
    console.error(err);
  }
}

async function fetchAgents() {
  const tbody = document.getElementById('agents-tbody');
  const summary = document.getElementById('agents-summary');
  try {
    const res = await fetch('/api/agents', {cache: 'no-store'});
    if (!res.ok) throw new Error('Agent 状态加载失败');
    const agents = await res.json();
    currentAgents = agents;
    summary.textContent = `（在线 ${agents.filter(a => a.is_online).length} / 共 ${agents.length}）`;
    if (!agents.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">暂无 Agent 连接，请在登录机器启动新版 Agent。</td></tr>';
      return;
    }
    const statuses = {waiting_auth: '等待有效认证', retrying: '上传失败，重试中', synced: '已同步', pending: '待同步'};
    tbody.innerHTML = agents.map((agent, index) => {
      const watches = (agent.watches || []).map(watch => {
        const profile = currentProfiles.find(p => p.id === watch.profile_id);
        const name = profile ? profile.name : watch.name || `账号 #${watch.profile_id || '?'}`;
        return `<div>${escapeHtml(name)} · ${escapeHtml(statuses[watch.status] || '未知')}</div>`;
      }).join('') || '尚未开始同步';
      const account = agent.current_account || {};
      const identity = account.email || account.account_id || (account.has_auth ? '无法识别账号，请手动关联' : '等待本机登录');
      const bindings = (agent.configured_watches || []).map(w => {
        const profile = currentProfiles.find(p => p.id === w.profile_id);
        return escapeHtml(profile ? profile.name : w.name || `账号 #${w.profile_id}`);
      }).join('、');
      return `<tr>
        <td><strong>${escapeHtml(agent.hostname)}</strong><div class="text-muted">${escapeHtml(agent.client_ip || '-')}</div></td>
        <td><span class="badge ${agent.is_online ? 'badge-online' : 'badge-idle'}">${agent.is_online ? '● 已连接' : '○ 离线'}</span></td>
        <td>${escapeHtml(identity)}${account.email && account.account_id ? `<div class="text-muted">${escapeHtml(account.account_id)}</div>` : ''}</td>
        <td><div>${bindings || '未匹配，请手动关联'} · ${agent.binding_mode === 'manual' ? '手动关联' : '自动匹配'}</div><div class="text-muted">${watches}</div></td>
        <td>${escapeHtml(new Date(agent.last_heartbeat).toLocaleString())}</td>
        <td>${escapeHtml(agent.version || '-')}</td>
        <td><button type="button" class="btn btn-secondary btn-sm" onclick="openAgentBinding(${index})">关联账号</button></td>
      </tr>`;
    }).join('');
  } catch (err) {
    summary.textContent = '（状态未知）';
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">无法获取 Agent 状态，请检查服务端连接及版本。</td></tr>';
    console.error(err);
  }
}

function openAgentBinding(index) {
  const agent = currentAgents[index];
  if (!agent) return;
  editingAgentId = agent.agent_id;
  const account = agent.current_account || {};
  document.getElementById('agent-binding-info').textContent = `${agent.hostname} · ${agent.client_ip || '-'} · 当前账号：${account.email || account.account_id || '未识别'}`;
  const select = document.getElementById('agent-binding-profile');
  select.innerHTML = '<option value="">自动匹配当前账号</option>' + currentProfiles.map(p =>
    `<option value="${p.id}">${escapeHtml(p.name)}${p.account_email ? ` · ${escapeHtml(p.account_email)}` : ''}</option>`).join('');
  select.value = agent.binding_mode === 'manual' && agent.configured_watches?.[0]?.profile_id
    ? String(agent.configured_watches[0].profile_id) : '';
  document.getElementById('modal-agent-binding').classList.add('active');
}

function closeAgentBinding() {
  document.getElementById('modal-agent-binding').classList.remove('active');
  editingAgentId = null;
}

document.getElementById('close-agent-binding').addEventListener('click', closeAgentBinding);
document.getElementById('cancel-agent-binding').addEventListener('click', closeAgentBinding);
document.getElementById('form-agent-binding').addEventListener('submit', async event => {
  event.preventDefault();
  if (!editingAgentId) return;
  const button = document.getElementById('save-agent-binding');
  const value = document.getElementById('agent-binding-profile').value;
  button.disabled = true;
  try {
    const res = await fetch(`/api/agents/${encodeURIComponent(editingAgentId)}/binding`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({profile_id: value ? Number(value) : null})
    });
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(typeof error.detail === 'string' ? error.detail : '关联保存失败');
    }
    closeAgentBinding();
    showToast('关联已保存，将在下一次心跳后生效', 'success');
    fetchAgents();
  } catch (err) { showToast(err.message, 'error'); }
  finally { button.disabled = false; }
});

function updateStats() {
  statTotalProfiles.textContent = currentProfiles.length;
  const totalActive = currentProfiles.reduce((acc, p) => acc + (p.active_users_count || 0), 0);
  statActiveUsers.textContent = totalActive;
}

function renderProfiles() {
  const openHistoryPanels = Array.from(profilesGrid.querySelectorAll('.quota-history[open]'));
  // 节点即使复用，脱离文档时浏览器仍会把内部滚动位置清零。
  const scrollPositions = Array.from(profilesGrid.querySelectorAll('.profile-card')).map(card => ({
    id: card.dataset.id,
    history: card.querySelector('.quota-history-entries')?.scrollTop || 0,
    users: card.querySelector('.users-list')?.scrollTop || 0
  }));
  const pageScroll = {left: window.scrollX, top: window.scrollY};
  const query = filterInput.value.trim().toLowerCase();
  const filtered = currentProfiles.filter(p => {
    if (!query) return true;
    const matchName = (p.name || "").toLowerCase().includes(query);
    const matchEmail = (p.account_email || "").toLowerCase().includes(query);
    const matchDesc = (p.description || "").toLowerCase().includes(query);
    const matchUser = (p.active_users || []).some(u => 
      (u.client_user || "").toLowerCase().includes(query) ||
      (u.client_hostname || "").toLowerCase().includes(query)
    );
    return matchName || matchEmail || matchDesc || matchUser;
  });

  if (filtered.length === 0) {
    profilesGrid.innerHTML = `
      <div style="grid-column: 1 / -1; text-align: center; padding: 48px; color: var(--text-muted);">
        <p>暂无符合条件的配置节点</p>
        <p style="font-size: 0.85rem; margin-top: 6px;">点击右上角「上传/更新认证」添加第一个配置</p>
      </div>
    `;
    return;
  }

  profilesGrid.innerHTML = filtered.map(p => {
    const hasUsers = p.active_users_count > 0;
    const limitText = p.max_users > 0 ? `限制 ${p.max_users} 人` : '无限制';
    const isFull = p.max_users > 0 && p.active_users_count >= p.max_users;

    const usersHtml = hasUsers ? p.active_users.map(u => {
      const displayName = u.display_name || u.client_user || "?";
      const initial = displayName.charAt(0).toUpperCase();
      const timeStr = u.connected_at ? new Date(u.connected_at).toLocaleTimeString() : "";
      const subInfo = (u.display_name && u.display_name !== u.client_user) 
        ? `${escapeHtml(u.client_user)} · ${escapeHtml(u.client_hostname)}` 
        : `${escapeHtml(u.client_hostname)}`;
      const statusPill = u.is_online 
        ? '<span style="display:inline-block; margin-left:4px; padding:1px 6px; font-size:0.68rem; border-radius:4px; background:rgba(16,185,129,0.15); color:#10b981; border:1px solid rgba(16,185,129,0.3);">在线</span>' 
        : '<span style="display:inline-block; margin-left:4px; padding:1px 6px; font-size:0.68rem; border-radius:4px; background:rgba(255,255,255,0.06); color:#94a3b8; border:1px solid rgba(255,255,255,0.1);">在用</span>';
      return `
        <div class="user-item">
          <div class="user-info">
            <div class="user-avatar">${initial}</div>
            <div class="user-names">
              <span class="user-name-primary">${escapeHtml(displayName)} ${statusPill}</span>
              <span class="user-host">${subInfo}</span>
            </div>
          </div>
          <span class="user-time">${timeStr} 连入</span>
        </div>
      `;
    }).join("") : '<div class="empty-users">目前暂无客户端连入</div>';

    const auth = p.auth || {};
    const authLabels = {valid: '认证有效', invalid: '认证失效', error: '暂时无法验证',
      unchecked: '认证未验证', disabled: '已停用', unsupported: '不支持验证'};
    const authStatus = Object.hasOwn(authLabels, auth.status) ? auth.status : 'unchecked';
    const authTime = auth.checked_at ? new Date(auth.checked_at).toLocaleString() : '尚未验证';
    const authHtml = `<div class="auth-status-row" role="status">
      <span class="badge auth-${authStatus}">${authLabels[authStatus]}</span>
      <span class="auth-check-time">${escapeHtml(authTime)}</span>
      ${auth.error ? `<div class="auth-status-error">${escapeHtml(auth.error)}</div>` : ''}
      ${authStatus === 'invalid' ? '<div class="auth-status-error">请重新登录并上传新的 auth.json</div>' : ''}
    </div>`;
    const q = p.quota || {};
    const windows = [q.five_hour, q.weekly].filter(w => w && w.available);
    const quotaHtml = q.has_data && windows.length ? `
      <div class="quota-section">
        ${windows.map(w => {
          if (w.remaining_percent == null) {
            return `<div class="quota-item"><span class="quota-name">${escapeHtml(w.name)}</span>
              <span class="quota-reset">官方未返回使用量</span></div>`;
          }
          const color = w.remaining_percent > 30 ? 'green' : (w.remaining_percent > 10 ? 'amber' : 'red');
          return `<div class="quota-item">
            <div class="quota-meta">
              <span class="quota-name">${escapeHtml(w.name)}</span>
              <span class="quota-percent ${color}">剩余 ${w.remaining_percent}% (已用 ${w.used_percent}%)</span>
            </div>
            <div class="progress-track"><div class="progress-fill ${color}" style="width: ${w.remaining_percent}%;"></div></div>
            <div class="quota-foot">
              <span>窗口: ${w.window_seconds === 604800 ? '7天' : '5小时'}</span>
              <span class="quota-reset">${w.reset_after_seconds == null ? '历史恢复样本不足' : w.reset_after_seconds === 0 ? '预估时间已到，等待实际恢复' : `预计 ${escapeHtml(w.reset_after_text)}后恢复`}</span>
            </div>
            <div class="quota-timestamp">历史预估恢复：${w.reset_source === 'history' ? escapeHtml(formatQuotaTime(w.reset_at)) : '暂无，需至少两次实际恢复记录'}</div>
            ${w.reset_source === 'history' ? `<div class="quota-timestamp">依据最近 ${w.recovery_sample_count} 次实际恢复，间隔约 ${escapeHtml(w.estimated_cycle_text)}${w.recovery_sample_count === 2 ? '（样本较少，仅供参考）' : '，仅供参考'}<br>最近观测到恢复：${escapeHtml(formatQuotaTime(w.last_recovered_at))}<br>按采样间隔推算：${escapeHtml(formatQuotaTime(w.prediction_range_start))} ～ ${escapeHtml(formatQuotaTime(w.prediction_range_end))}</div>` : ''}
          </div>`;
        }).join('')}
      </div>
    ` : `
      <div class="quota-section" style="text-align: center; color: var(--text-muted); font-size: 0.8rem; padding: 10px;">
        <span>暂无可用额度数据，请刷新额度</span>
      </div>
    `;

    const quotaHistoryHtml = `<div class="quota-timestamp">额度查询：${escapeHtml(formatQuotaTime(q.last_updated))}（北京时间）</div>
      <details id="quota-history-panel-${p.id}" class="quota-history" ontoggle="if(this.open) loadQuotaHistory(${p.id})">
        <summary>额度查询历史</summary>
        <label>查询日期（北京时间） <input type="date" id="quota-history-day-${p.id}"
          value="${new Date().toLocaleDateString('sv-SE', {timeZone: 'Asia/Shanghai'})}"
          onchange="loadQuotaHistory(${p.id})"></label>
        <div>每天最多显示最近 2000 次查询；用量下降不一定代表周期重置。</div>
        <div id="quota-history-${p.id}" class="quota-history-entries"></div>
      </details>`;

    return `
      <div class="profile-card" data-id="${p.id}">
        <div class="profile-head">
          <div class="profile-title-group">
            <div class="profile-name-row">
              <div class="profile-name">${escapeHtml(p.name)}</div>
              <button class="profile-rename" onclick="renameProfile(${p.id})" title="修改账号名称" aria-label="修改账号名称">✎ 编辑</button>
            </div>
            <div class="profile-desc">${escapeHtml(p.description || "暂无描述")}</div>
          </div>
          <span class="badge ${hasUsers ? 'badge-online' : 'badge-idle'}">
            ${hasUsers ? `● 在线 ${p.active_users_count} 人` : '○ 闲置中'}
          </span>
        </div>

        <div class="profile-badges">
          ${p.plan_type ? `<span class="badge badge-plan">🏆 ${escapeHtml(formatPlanName(p.plan_type))}</span>` : ''}
          ${p.account_email ? `<span class="badge badge-email">📧 ${escapeHtml(p.account_email)}</span>` : ''}
          <span class="badge badge-limit">${limitText}</span>
          ${isFull ? `<span class="badge" style="background: rgba(239, 68, 68, 0.2); color: #f87171;">已满额</span>` : ''}
        </div>

        ${authHtml}
        ${(authStatus === 'invalid' || authStatus === 'error') && q.has_data ? '<div class="auth-status-row auth-check-time">下方额度为上次成功查询的缓存</div>' : ''}
        ${quotaHtml}
        ${quotaHistoryHtml}

        <div class="users-section">
          <div class="users-title">
            <span>当前使用者清单 (${p.active_users_count})</span>
            <span style="font-weight: normal; font-size: 0.72rem;">90s 心跳活跃</span>
          </div>
          <div class="users-list">
            ${usersHtml}
          </div>
        </div>

        <div class="profile-actions">
          <button class="btn btn-secondary btn-sm" onclick="manualRefreshQuota(${p.id})" title="向 OpenAI 请求最新实时额度">
            🔄 刷新额度
          </button>
          <div class="action-btns">
            <button class="btn btn-primary btn-sm" onclick="triggerReplaceJson(${p.id})" title="直接上传新的 auth.json 替换本账号的认证凭据">
              📤 替换 auth.json
            </button>
            <button class="btn btn-danger btn-sm" onclick="confirmDeleteProfile(${p.id})">
              删除
            </button>
          </div>
        </div>
      </div>
    `;
  }).join("");
  // 定时刷新卡片时保留历史面板、所选日期和滚动位置。
  for (const panel of openHistoryPanels) {
    const replacement = document.getElementById(panel.id);
    if (replacement) replacement.replaceWith(panel);
  }
  for (const position of scrollPositions) {
    const card = Array.from(profilesGrid.querySelectorAll('.profile-card'))
      .find(element => element.dataset.id === position.id);
    if (!card) continue;
    const history = card.querySelector('.quota-history-entries');
    const users = card.querySelector('.users-list');
    if (history) history.scrollTop = position.history;
    if (users) users.scrollTop = position.users;
  }
  window.scrollTo(pageScroll);
}

function renderLogs(logs) {
  if (!logs || logs.length === 0) {
    logsTbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted">暂无操作日志</td></tr>`;
    return;
  }

  logsTbody.innerHTML = logs.map(l => {
    const timeStr = l.created_at ? new Date(l.created_at).toLocaleString() : "";
    return `
      <tr>
        <td style="font-family: var(--font-mono); font-size: 0.75rem; color: #94a3b8;">${timeStr}</td>
        <td><span class="badge-action ${l.action}">${escapeHtml(l.action)}</span></td>
        <td><strong>${escapeHtml(l.profile_name || '-')}</strong></td>
        <td>${escapeHtml(l.client_user || '-')}</td>
        <td style="font-family: var(--font-mono);">${escapeHtml(l.client_hostname || '-')}</td>
        <td style="font-family: var(--font-mono);">${escapeHtml(l.client_ip || '-')}</td>
        <td style="color: var(--text-muted);">${escapeHtml(l.details || '-')}</td>
      </tr>
    `;
  }).join("");
}

async function renameProfile(profileId) {
  const profile = currentProfiles.find(p => p.id === profileId);
  if (!profile) return;
  const input = prompt('输入新的账号名称（最多 100 个字符）', profile.name || '');
  if (input === null) return;
  const name = input.trim();
  if (!name || Array.from(name).length > 100) {
    showToast('账号名称需为 1 至 100 个字符', 'error');
    return;
  }
  if (name === profile.name) return;
  try {
    const res = await fetch(`/api/profiles/${profileId}/name`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || '名称修改失败');
    showToast('账号名称已修改', 'success');
    await fetchProfiles();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function manualRefreshQuota(profileId) {
  try {
    showToast("正在查询额度，剩余100%时将发送周期触发请求...", "info");
    const res = await fetch(`/api/profiles/${profileId}/refresh-quota`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "刷新失败");
    }
    const result = await res.json();
    const trigger = result.quota?.cycle_trigger;
    if (trigger && !trigger.success) {
      showToast(`额度已同步；${trigger.error}`, "error");
    } else if (!trigger || trigger.skipped) {
      showToast("额度已同步为官方最新数据", "success");
    } else {
      showToast("额度已同步，周期触发请求已完成", "success");
    }
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    await fetchProfiles();
  }
}

let currentReplacingProfileId = null;
let currentReplacingProfileName = "";
const replaceFileInput = document.getElementById("replace-file-input");
const usersTbody = document.getElementById("users-tbody");

function triggerReplaceJson(profileId) {
  const profileName = currentProfiles.find(p => p.id === profileId)?.name || '';
  currentReplacingProfileId = profileId;
  currentReplacingProfileName = profileName;
  if (replaceFileInput) {
    replaceFileInput.value = "";
    replaceFileInput.click();
  }
}

if (replaceFileInput) {
  replaceFileInput.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file || !currentReplacingProfileId) return;

    if (!confirm(`确定要使用文件 "${file.name}" 替换节点 "${currentReplacingProfileName}" 的现有 auth.json 认证吗？\n该操作会立即更新该账号凭据并重新拉取官方额度。`)) {
      replaceFileInput.value = "";
      return;
    }

    const formData = new FormData();
    formData.append("file", file);

    showToast(`正在上传并替换 ${currentReplacingProfileName} 的凭据...`, "info");
    try {
      const res = await fetch(`/api/profiles/${currentReplacingProfileId}/replace-file`, {
        method: "POST",
        body: formData
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "替换失败");
      }
      showToast(`已成功替换 ${currentReplacingProfileName} 的 auth.json，正在同步最新额度！`, "success");
      fetchProfiles();
      fetchLogs();
      fetchTeamUsers();
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      replaceFileInput.value = "";
    }
  });
}

async function fetchTeamUsers() {
  if (!usersTbody) return;
  try {
    const res = await fetch("/api/users");
    if (!res.ok) return;
    const users = await res.json();
    renderTeamUsers(users);
  } catch (err) {
    console.error(err);
  }
}

function renderTeamUsers(users) {
  if (!usersTbody) return;
  if (!users || users.length === 0) {
    usersTbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted">暂无团队成员数据</td></tr>`;
    return;
  }
  usersTbody.innerHTML = users.map(u => {
    const isOnline = !!u.active_profile;
    const lastLogin = u.last_login ? new Date(u.last_login).toLocaleString() : "从未登录";
    const connTime = u.active_profile && u.active_profile.connected_at ? new Date(u.active_profile.connected_at).toLocaleTimeString() : "-";
    const statusHtml = isOnline 
      ? `<span class="badge badge-online">● 正在使用: <strong>${escapeHtml(u.active_profile.name)}</strong></span>` 
      : `<span class="badge badge-idle">○ 闲置未占用</span>`;

    let actions = `<div style="text-align: right; display: flex; gap: 6px; justify-content: flex-end;">`;
    if (isOnline) {
      actions += `<button class="btn btn-secondary btn-sm" onclick="kickUser('${escapeHtml(u.username)}')" title="强制释放该用户占用的账号">⏹ 释放</button>`;
    }
    if (u.username !== "admin") {
      actions += `<button class="btn btn-danger btn-sm" onclick="deleteUser(${u.id}, '${escapeHtml(u.username)}')">删除</button>`;
    }
    actions += `</div>`;

    return `
      <tr>
        <td style="font-family: var(--font-mono); font-weight: 600;">${escapeHtml(u.username)}</td>
        <td>${escapeHtml(u.display_name || u.username)}</td>
        <td><span class="badge ${u.role === 'admin' ? 'badge-plan' : 'badge-limit'}">${escapeHtml(u.role)}</span></td>
        <td>${statusHtml}</td>
        <td style="font-family: var(--font-mono); font-size: 0.8rem; color: #94a3b8;">${connTime}</td>
        <td style="font-family: var(--font-mono); font-size: 0.8rem; color: #94a3b8;">${lastLogin}</td>
        <td>${actions}</td>
      </tr>
    `;
  }).join("");
}

async function kickUser(username) {
  if (!confirm(`确定要强制释放用户 "${username}" 当前占用的 Codex 账号吗？\n释放后其他成员可立即使用该账号。`)) return;
  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(username)}/kick`, { method: "POST" });
    if (!res.ok) throw new Error("操作失败");
    showToast(`已成功释放用户 "${username}" 的账号占用`, "success");
    fetchProfiles();
    fetchLogs();
    fetchTeamUsers();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function deleteUser(userId, username) {
  if (!confirm(`确定要删除团队用户 "${username}" 吗？此操作不可逆。`)) return;
  try {
    const res = await fetch(`/api/admin/users/${userId}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "删除失败");
    }
    showToast(`用户 "${username}" 已删除`, "success");
    fetchTeamUsers();
    fetchProfiles();
    fetchLogs();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function confirmDeleteProfile(profileId) {
  const profileName = currentProfiles.find(p => p.id === profileId)?.name || '';
  if (!confirm(`确定要删除服务端配置 "${profileName}" 吗？此操作不可逆。`)) return;
  try {
    const res = await fetch(`/api/profiles/${profileId}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "删除失败");
    }
    showToast(`配置 "${profileName}" 已成功删除`, "success");
    fetchProfiles();
    fetchLogs();
    fetchTeamUsers();
  } catch (err) {
    showToast(err.message, "error");
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// 标签页切换
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    const tabId = btn.dataset.tab;
    document.getElementById(tabId).classList.add("active");
  });
});

// 文件上传交互
dropZone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", (e) => {
  if (e.target.files.length > 0) {
    selectedFile = e.target.files[0];
    fileNameDisplay.textContent = `已选文件: ${selectedFile.name} (${Math.round(selectedFile.size / 1024)} KB)`;
  }
});
dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("dragover");
});
dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("dragover");
});
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("dragover");
  if (e.dataTransfer.files.length > 0) {
    selectedFile = e.dataTransfer.files[0];
    fileNameDisplay.textContent = `已选文件: ${selectedFile.name} (${Math.round(selectedFile.size / 1024)} KB)`;
  }
});

// 模态弹窗控制
btnOpenUpload.addEventListener("click", () => {
  modalUpload.classList.add("active");
});
btnCloseModal.addEventListener("click", () => modalUpload.classList.remove("active"));
btnCancelModal.addEventListener("click", () => modalUpload.classList.remove("active"));

// 表单提交
formUpload.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("input-name").value.trim();
  const desc = document.getElementById("input-desc").value.trim();
  const maxUsers = parseInt(document.getElementById("input-max-users").value, 10) || 0;

  const activeTab = document.querySelector(".tab-btn.active").dataset.tab;

  try {
    if (activeTab === "tab-file") {
      if (!selectedFile) {
        throw new Error("请先选择或拖拽 auth.json 文件");
      }
      const formData = new FormData();
      formData.append("name", name);
      formData.append("description", desc);
      formData.append("max_users", maxUsers);
      formData.append("file", selectedFile);

      const res = await fetch("/api/profiles/upload-file", {
        method: "POST",
        body: formData
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "上传失败");
      }
    } else {
      const jsonText = document.getElementById("json-textarea").value.trim();
      if (!jsonText) throw new Error("请输入 JSON 格式内容");
      let authObj;
      try {
        authObj = JSON.parse(jsonText);
      } catch (err) {
        throw new Error("JSON 格式错误，请检查输入");
      }

      const res = await fetch("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name,
          description: desc,
          max_users: maxUsers,
          auth_json: authObj
        })
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "上传失败");
      }
    }

    showToast("配置保存成功！", "success");
    modalUpload.classList.remove("active");
    formUpload.reset();
    selectedFile = null;
    fileNameDisplay.textContent = "";
    fetchProfiles();
    fetchLogs();
    fetchTeamUsers();
  } catch (err) {
    showToast(err.message, "error");
  }
});

// 管理员添加用户弹窗交互
const modalAddUser = document.getElementById("modal-add-user");
const btnOpenAddUser = document.getElementById("btn-open-add-user");
const btnCloseUserModal = document.getElementById("btn-close-user-modal");
const btnCancelUserModal = document.getElementById("btn-cancel-user-modal");
const formAddUser = document.getElementById("form-add-user");

if (btnOpenAddUser) {
  btnOpenAddUser.addEventListener("click", () => {
    formAddUser.reset();
    modalAddUser.classList.add("active");
  });
}

function closeUserModal() {
  if (modalAddUser) modalAddUser.classList.remove("active");
}
if (btnCloseUserModal) btnCloseUserModal.addEventListener("click", closeUserModal);
if (btnCancelUserModal) btnCancelUserModal.addEventListener("click", closeUserModal);

if (formAddUser) {
  formAddUser.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("input-user-name").value.trim();
    const password = document.getElementById("input-user-pwd").value.trim();
    const displayName = document.getElementById("input-user-display").value.trim();
    const role = document.getElementById("select-user-role").value;

    if (!username || !password) {
      showToast("用户名和密码不能为空", "error");
      return;
    }

    try {
      showToast(`正在添加新用户 ${username}...`, "info");
      const res = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: username,
          password: password,
          display_name: displayName,
          role: role
        })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "添加失败");
      }
      showToast(`用户 "${username}" 创建成功！`, "success");
      closeUserModal();
      formAddUser.reset();
      fetchTeamUsers();
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

filterInput.addEventListener("input", renderProfiles);
btnRefresh.addEventListener("click", () => {
  fetchAgents();
  fetchProfiles();
  fetchLogs();
  fetchTeamUsers();
  showToast("数据已刷新", "info");
});

// 首次加载与每5秒自动轮询刷新
fetchAgents();
fetchProfiles();
fetchLogs();
fetchTeamUsers();
setInterval(() => {
  fetchAgents();
  fetchProfiles({background: true});
  fetchLogs();
  fetchTeamUsers();
}, 5000);
