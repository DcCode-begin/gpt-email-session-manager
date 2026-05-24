const state = {
  emails: [],
  selected: new Set(),
  groups: [],
  settings: {
    clipboard_clear_seconds: "30",
    chatgpt_proxy: "",
  },
  importRows: [],
  importPreview: null,
  chatgptLoginJobId: "",
  chatgptLoginPollTimer: 0,
  chatgptBulkLoginJobId: "",
  chatgptBulkLoginPollTimer: 0,
  chatgptBulkLoginSavedCount: 0,
};

const $ = (id) => document.getElementById(id);
const CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session";
const ISOLATED_GROUPS = ["失效账号"];
const INVALID_GROUP = "失效账号";
const LOGIN_DONE_STATUSES = ["ok", "failed", "closed", "banned", "retry"];
const LOGIN_PROBLEM_STATUSES = ["failed", "closed", "banned", "retry"];

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  await refreshStatus();
}

function bindEvents() {
  $("addEmailBtn").addEventListener("click", () => openEmailModal());
  $("emailForm").addEventListener("submit", submitEmailForm);
  $("openImportBtn").addEventListener("click", openImportModal);
  $("importFile").addEventListener("change", handleImportFile);
  $("commitImportBtn").addEventListener("click", commitImport);
  $("exportMaskedBtn").addEventListener("click", () => exportEmails("masked", false));
  $("exportFullBtn").addEventListener("click", () => exportEmails("full", false));
  $("copyChatgptSessionBtn").addEventListener("click", copyChatgptSessionUrl);
  $("copyAllChatgptSessionsBtn").addEventListener("click", copyAllChatgptSessions);
  $("exportChatgptSessionsBtn").addEventListener("click", exportChatgptSessions);
  $("bulkExportBtn").addEventListener("click", () => exportEmails("masked", true));
  $("groupsBtn").addEventListener("click", openGroups);
  $("groupForm").addEventListener("submit", createGroup);
  $("settingsBtn").addEventListener("click", openSettings);
  $("settingsForm").addEventListener("submit", saveSettings);
  $("logsBtn").addEventListener("click", openLogs);
  $("selectAll").addEventListener("change", toggleSelectAll);
  $("selectWithSessionBtn").addEventListener("click", () => selectByChatgptSession(true));
  $("selectWithoutSessionBtn").addEventListener("click", () => selectByChatgptSession(false));
  $("selectRetryBtn").addEventListener("click", () => selectByStatus("retry"));
  $("selectBannedBtn").addEventListener("click", () => selectByStatus("banned"));
  $("bulkDeleteBtn").addEventListener("click", bulkDelete);
  $("bulkGroupBtn").addEventListener("click", bulkGroup);
  $("bulkTestBtn").addEventListener("click", bulkTest);
  $("bulkFetchBtn").addEventListener("click", bulkFetchCodes);
  $("bulkChatgptLoginBtn").addEventListener("click", bulkChatgptLogin);
  $("bulkCopySessionBtn").addEventListener("click", copySelectedChatgptSessions);
  $("bulkExportSessionBtn").addEventListener("click", exportSelectedChatgptSessions);
  $("bulkInvalidBtn").addEventListener("click", bulkInvalid);
  $("bulkRestoreGroupBtn").addEventListener("click", bulkRestoreGroup);
  $("resetFiltersBtn").addEventListener("click", resetFilters);

  for (const id of ["searchInput", "groupFilter", "statusFilter"]) {
    $(id).addEventListener("input", debounce(loadEmails, 180));
  }

  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = $(button.dataset.close);
      if (dialog.open) dialog.close();
    });
  });
}

async function refreshStatus() {
  const data = await plainGet("/api/status");
  state.settings = data.settings || state.settings;

  showApp();
  await loadEmails();
}

function showApp() {
  $("appShell").classList.remove("hidden");
}

async function loadEmails() {
  const query = new URLSearchParams({
    search: $("searchInput").value.trim(),
    group_name: $("groupFilter").value,
    status: $("statusFilter").value,
  });
  const [emailData, groupData] = await Promise.all([
    api(`/api/emails?${query.toString()}`),
    api("/api/groups"),
  ]);
  state.emails = emailData.emails || [];
  state.groups = groupData.groups || [];
  renderFilters();
  renderEmails();
  renderGroups();
}

function renderFilters() {
  fillSelect(
    $("groupFilter"),
    uniqueValues([
      ...state.groups.map((item) => item.name).filter(Boolean),
      ...state.emails.map((item) => item.group_name).filter(Boolean),
      ...ISOLATED_GROUPS,
    ]),
    "全部",
  );
}

function fillSelect(select, values, allLabel) {
  const current = select.value;
  select.innerHTML = `<option value="">${allLabel}</option>`;
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  select.value = values.includes(current) ? current : "";
}

function renderEmails() {
  const body = $("emailTableBody");
  $("emailCount").textContent = state.emails.length;
  $("emptyState").classList.toggle("hidden", state.emails.length > 0);
  state.selected = new Set([...state.selected].filter((id) => state.emails.some((item) => item.id === id)));
  body.innerHTML = state.emails.map((email) => emailRow(email)).join("");
  body.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", handleTableAction);
  });
  body.querySelectorAll("[data-select]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const id = Number(checkbox.dataset.select);
      checkbox.checked ? state.selected.add(id) : state.selected.delete(id);
      renderSelection();
    });
  });
  renderSelection();
}

function emailRow(email) {
  const password = email.password || "";
  const code = email.latest_code || "";
  const lastCheck = email.last_check_at || "未检测";
  const statusClass = escapeAttr(email.status || "unknown");
  const statusError = email.last_error ? shortText(email.last_error, 90) : "";
  const oauthBadge = email.has_oauth ? '<span class="tiny-badge">OAuth</span>' : "";
  const sessionBadge = email.has_chatgpt_session ? '<span class="tiny-badge session">Session</span>' : "";
  const isInvalidGroup = ISOLATED_GROUPS.includes(email.group_name || "");
  const restoreTarget = email.previous_group_name || email.provider || "未分组";
  const sessionTitle = email.has_chatgpt_session
    ? `上次保存：${email.chatgpt_session_updated_at || "未知时间"}`
    : "这个账号还没有保存 session，请先点 GPT登录";
  return `
    <tr>
      <td><input data-select="${email.id}" type="checkbox" ${state.selected.has(email.id) ? "checked" : ""}></td>
      <td>
        <div class="account-cell">
          <span class="mono account-address copyable-text" data-action="copy-email" data-id="${email.id}" title="点击复制邮箱">${escapeHtml(email.email)}</span>
          ${sessionBadge}
        </div>
      </td>
      <td>
        <span class="mono copyable-text password-text" data-action="copy-password-direct" data-id="${email.id}" title="点击复制密码">${escapeHtml(password)}</span>
      </td>
      <td>${email.group_name ? `<span class="group-chip">${escapeHtml(email.group_name)}</span> ${oauthBadge}` : `<span class="group-chip">其他</span> ${oauthBadge}`}</td>
      <td>
        <div class="status-cell">
          <span class="status ${statusClass}" title="${escapeAttr(email.last_error || "")}">${statusText(email.status)}</span>
          ${statusError ? `<span class="status-error" title="${escapeAttr(email.last_error)}">${escapeHtml(statusError)}</span>` : ""}
        </div>
      </td>
      <td>
        ${code ? `<span class="mono copyable-text" data-action="copy-code" data-id="${email.id}" title="点击复制验证码">${escapeHtml(code)}</span>` : '<span class="muted">无</span>'}
      </td>
      <td><span class="muted">${escapeHtml(lastCheck)}</span></td>
      <td>
        <div class="action-grid">
          <button class="mini" data-action="test-imap" data-id="${email.id}">检测</button>
          <button class="mini" data-action="fetch-code" data-id="${email.id}">验证码</button>
          <button class="mini" data-action="chatgpt-login" data-id="${email.id}">GPT登录</button>
          <button class="mini" data-action="copy-chatgpt-session" data-id="${email.id}" title="${escapeAttr(sessionTitle)}" ${email.has_chatgpt_session ? "" : "disabled"}>复制Session</button>
          <button class="mini danger-light" data-action="mark-invalid" data-id="${email.id}" ${isInvalidGroup ? "disabled" : ""}>设为失效</button>
          ${isInvalidGroup ? `<button class="mini" data-action="restore-group" data-id="${email.id}" title="恢复到：${escapeAttr(restoreTarget)}">恢复分组</button>` : ""}
          <button class="mini" data-action="edit" data-id="${email.id}">编辑</button>
          <button class="mini danger-light" data-action="delete" data-id="${email.id}">删除</button>
        </div>
      </td>
    </tr>
  `;
}

async function handleTableAction(event) {
  const button = event.currentTarget;
  const id = Number(button.dataset.id);
  const action = button.dataset.action;
  const email = state.emails.find((item) => item.id === id);
  try {
    if (action === "copy-email") {
      await copyText(email.email, false);
      await api("/api/log", { method: "POST", body: { action: "copy_email", email_id: id } });
    }
    if (action === "copy-password-direct") {
      await copyText(email.password || "", true, "密码已复制。");
      await api("/api/log", { method: "POST", body: { action: "copy_password", email_id: id } });
    }
    if (action === "copy-code") {
      await copyText(email.latest_code, true);
      await api(`/api/emails/${id}/copy-code`, { method: "POST" });
    }
    if (action === "test-imap") {
      button.disabled = true;
      const result = await api(`/api/emails/${id}/test-imap`, { method: "POST" });
      await loadEmails();
      toast(result.email.status === "ok" ? "检测成功。" : `检测失败：${shortText(result.email.last_error, 120)}`, result.email.status !== "ok");
    }
    if (action === "fetch-code") {
      button.disabled = true;
      const result = await api(`/api/emails/${id}/fetch-code`, { method: "POST" });
      await loadEmails();
      toast(result.code ? "验证码已刷新。" : `验证码读取失败：${shortText(result.error || "未找到验证码", 120)}`, !result.code);
    }
    if (action === "chatgpt-login") {
      button.disabled = true;
      const result = await api(`/api/emails/${id}/chatgpt-login`, { method: "POST" });
      monitorChatgptLogin(result.job);
      toast(result.job?.message || "已启动 GPT 登录。");
    }
    if (action === "copy-chatgpt-session") {
      const data = await api(`/api/emails/${id}/copy-chatgpt-session`, { method: "POST" });
      await copyText(data.session, true, "这个账号的 ChatGPT session 已复制。");
    }
    if (action === "mark-invalid") {
      if (!confirm(`将 ${email.email} 移入 ${INVALID_GROUP} 分组？`)) return;
      await api("/api/bulk-group", { method: "POST", body: { ids: [id], group_name: INVALID_GROUP } });
      state.selected.delete(id);
      await loadEmails();
      toast(`已移入 ${INVALID_GROUP}。`);
    }
    if (action === "restore-group") {
      const target = email.previous_group_name || email.provider || "未分组";
      if (!confirm(`将 ${email.email} 恢复到 ${target}？`)) return;
      const result = await api(`/api/emails/${id}/restore-group`, { method: "POST" });
      state.selected.delete(id);
      await loadEmails();
      toast(result.restored ? `已恢复到 ${target}。` : "这个账号不在失效分组中。");
    }
    if (action === "edit") {
      openEmailModal(email);
    }
    if (action === "delete") {
      if (confirm(`删除 ${email.email}？`)) {
        await api(`/api/emails/${id}`, { method: "DELETE" });
        state.selected.delete(id);
        await loadEmails();
      }
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    if ("disabled" in button) button.disabled = false;
  }
}

function renderSelection() {
  const count = state.selected.size;
  $("selectedCount").textContent = `已选 ${count}`;
  $("selectAll").checked = count > 0 && count === state.emails.length;
  $("selectWithSessionBtn").disabled = state.emails.length === 0;
  $("selectWithoutSessionBtn").disabled = state.emails.length === 0;
  $("bulkRestoreGroupBtn").classList.toggle("hidden", $("groupFilter").value !== INVALID_GROUP);
  ["bulkGroupBtn", "bulkTestBtn", "bulkFetchBtn", "bulkChatgptLoginBtn", "bulkCopySessionBtn", "bulkExportSessionBtn", "bulkExportBtn", "bulkInvalidBtn", "bulkRestoreGroupBtn", "bulkDeleteBtn"].forEach((id) => {
    $(id).disabled = count === 0 || (id === "bulkChatgptLoginBtn" && isBulkChatgptLoginActive());
  });
  ["selectWithSessionBtn", "selectWithoutSessionBtn", "selectRetryBtn", "selectBannedBtn"].forEach((id) => {
    $(id).disabled = state.emails.length === 0;
  });
  $("bulkLoginConcurrency").disabled = isBulkChatgptLoginActive();
}

function isBulkChatgptLoginActive() {
  return Boolean(state.chatgptBulkLoginJobId);
}

function toggleSelectAll(event) {
  if (event.target.checked) {
    state.emails.forEach((item) => state.selected.add(item.id));
  } else {
    state.selected.clear();
  }
  renderEmails();
}

function selectByChatgptSession(hasSession) {
  state.selected.clear();
  state.emails
    .filter((item) => Boolean(item.has_chatgpt_session) === hasSession)
    .forEach((item) => state.selected.add(item.id));
  renderEmails();
  toast(`已选择 ${state.selected.size} 个${hasSession ? "有" : "无"} Session 的账号。`);
}

function selectByStatus(status) {
  state.selected.clear();
  state.emails
    .filter((item) => item.status === status)
    .forEach((item) => state.selected.add(item.id));
  renderEmails();
  toast(`已选择 ${state.selected.size} 个${statusText(status)}账号。`);
}

function openEmailModal(email = null) {
  $("emailModalTitle").textContent = email ? "编辑邮箱" : "新增邮箱";
  $("emailId").value = email?.id || "";
  $("emailField").value = email?.email || "";
  $("passwordField").value = "";
  $("passwordField").required = !email;
  $("passwordField").placeholder = email ? "留空则不修改" : "";
  $("groupField").value = email?.group_name || "";
  $("imapHostField").value = email?.imap_host || "";
  $("imapPortField").value = email?.imap_port || 993;
  $("tagsField").value = email?.tags || "";
  $("remarkField").value = email?.remark || "";
  $("emailModal").showModal();
}

async function submitEmailForm(event) {
  event.preventDefault();
  const id = $("emailId").value;
  const body = {
    email: $("emailField").value,
    password: $("passwordField").value,
    group_name: $("groupField").value,
    imap_host: $("imapHostField").value,
    imap_port: $("imapPortField").value,
    tags: $("tagsField").value,
    remark: $("remarkField").value,
  };
  try {
    if (id) {
      await api(`/api/emails/${id}`, { method: "PUT", body });
    } else {
      await api("/api/emails", { method: "POST", body });
    }
    $("emailModal").close();
    await loadEmails();
    toast("已保存。");
  } catch (error) {
    toast(error.message, true);
  }
}

function openImportModal() {
  state.importRows = [];
  state.importPreview = null;
  $("importFile").value = "";
  $("importSummary").textContent = "";
  $("importPreviewBody").innerHTML = "";
  $("commitImportBtn").disabled = true;
  $("importModal").showModal();
}

async function handleImportFile(event) {
  const file = event.target.files[0];
  if (!file) return;
  const text = await file.text();
  try {
    state.importRows = rowsFromImport(text, file.name);
    const preview = await api("/api/import/preview", {
      method: "POST",
      body: { rows: state.importRows },
    });
    state.importPreview = preview;
    renderImportPreview(preview);
  } catch (error) {
    $("importSummary").textContent = error.message;
    $("commitImportBtn").disabled = true;
  }
}

function renderImportPreview(preview) {
  const updateCount = preview.rows.filter((item) => item.mode === "update" && !item.errors.length).length;
  $("importSummary").textContent = `可处理 ${preview.valid_count} 行，其中更新 ${updateCount} 行，错误 ${preview.error_count} 行。`;
  $("commitImportBtn").disabled = preview.valid_count === 0;
  $("importPreviewBody").innerHTML = preview.rows.map((item) => {
    const errors = item.errors.join("；");
    return `
      <tr>
        <td>${item.row}</td>
        <td>${escapeHtml(item.data.email)}</td>
        <td>${escapeHtml(item.data.group_name)}</td>
        <td class="${errors ? "error-text" : "ok-text"}">${errors || (item.mode === "update" ? "更新已有" : "可导入")}</td>
      </tr>
    `;
  }).join("");
}

async function commitImport() {
  try {
    const result = await api("/api/import/commit", {
      method: "POST",
      body: { rows: state.importRows },
    });
    $("importModal").close();
    await loadEmails();
    toast(`导入完成：新增 ${result.created}，更新 ${result.updated || 0}，跳过 ${result.skipped}。`);
  } catch (error) {
    toast(error.message, true);
  }
}

function exportEmails(mode, selectedOnly) {
  if (mode === "full" && !confirm("完整导出会包含密码或授权码，确定继续？")) return;
  const ids = selectedOnly ? [...state.selected] : [];
  const query = new URLSearchParams({ mode });
  if (ids.length) query.set("ids", ids.join(","));
  window.location.href = `/api/export?${query.toString()}`;
}

async function copyAllChatgptSessions() {
  if (!confirm("将复制所有已保存的 ChatGPT session，格式为可导入 JSON 数组。Session 等同登录凭证，请只粘贴到你自己的工具里。确定继续？")) return;
  await copyChatgptSessions([]);
}

async function copySelectedChatgptSessions() {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`将复制选中的 ${ids.length} 个账号里已保存的 ChatGPT session。确定继续？`)) return;
  await copyChatgptSessions(ids);
}

async function copyChatgptSessions(ids = []) {
  try {
    const data = await api("/api/chatgpt-sessions/copy-all", {
      method: "POST",
      body: ids.length ? { ids } : {},
    });
    if (data.clipboard_copied) {
      toast(`已复制 ${data.count} 个可导入 ChatGPT session，请直接粘贴到导入框。`);
      return;
    }
    await navigator.clipboard.writeText(data.text || "");
    toast(`浏览器已复制 ${data.count} 个可导入 ChatGPT session，请直接粘贴到导入框。`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function exportChatgptSessions() {
  if (!confirm("导出的 JSON 文件会包含所有已保存的 ChatGPT session，格式为可导入 JSON 数组。请妥善保管，确定继续？")) return;
  await exportChatgptSessionsByIds([]);
}

async function exportSelectedChatgptSessions() {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`将导出选中的 ${ids.length} 个账号里已保存的 ChatGPT session。确定继续？`)) return;
  await exportChatgptSessionsByIds(ids);
}

async function exportChatgptSessionsByIds(ids = []) {
  try {
    const query = new URLSearchParams();
    if (ids.length) query.set("ids", ids.join(","));
    const response = await fetch(`/api/chatgpt-sessions/export?${query.toString()}`);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "Session 文件导出失败。");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filenameFromDisposition(
      response.headers.get("Content-Disposition"),
      `chatgpt_sessions_import_${timestampForFile()}.json`,
    );
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("可导入 Session 文件已导出。");
  } catch (error) {
    toast(error.message, true);
  }
}

async function bulkDelete() {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`删除选中的 ${ids.length} 个邮箱？`)) return;
  try {
    await api("/api/bulk-delete", { method: "POST", body: { ids } });
    state.selected.clear();
    await loadEmails();
    toast("已删除。");
  } catch (error) {
    toast(error.message, true);
  }
}

async function bulkGroup() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const groupName = prompt("输入新的分组名称：", "");
  if (groupName === null) return;
  try {
    await api("/api/bulk-group", { method: "POST", body: { ids, group_name: groupName } });
    await loadEmails();
    toast("分组已更新。");
  } catch (error) {
    toast(error.message, true);
  }
}

async function bulkTest() {
  const ids = [...state.selected];
  if (!ids.length) return;
  try {
    toast("正在批量检测...");
    await api("/api/bulk-test", { method: "POST", body: { ids } });
    await loadEmails();
    toast("批量检测完成。");
  } catch (error) {
    toast(error.message, true);
  }
}

async function bulkFetchCodes() {
  const ids = [...state.selected];
  if (!ids.length) return;
  try {
    toast("正在批量刷新验证码...");
    await api("/api/bulk-fetch-codes", { method: "POST", body: { ids } });
    await loadEmails();
    toast("验证码刷新完成。");
  } catch (error) {
    toast(error.message, true);
  }
}

async function bulkChatgptLogin() {
  const ids = [...state.selected];
  if (!ids.length) return;
  try {
    toast(`已启动批量 GPT 登录：${ids.length} 个账号。`);
    const result = await api("/api/bulk-chatgpt-login", {
      method: "POST",
      body: { ids, concurrency: Number($("bulkLoginConcurrency").value || 3) },
    });
    monitorBulkChatgptLogin(result.job);
  } catch (error) {
    toast(error.message, true);
  }
}

async function bulkInvalid() {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`将选中的 ${ids.length} 个账号移入 ${INVALID_GROUP} 分组？`)) return;
  try {
    await api("/api/bulk-group", { method: "POST", body: { ids, group_name: INVALID_GROUP } });
    state.selected.clear();
    await loadEmails();
    toast(`已移入 ${INVALID_GROUP}。`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function bulkRestoreGroup() {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`恢复选中的 ${ids.length} 个账号到原分组？`)) return;
  try {
    const result = await api("/api/bulk-restore-group", { method: "POST", body: { ids } });
    state.selected.clear();
    await loadEmails();
    toast(`已恢复 ${result.restored || 0} 个账号。`);
  } catch (error) {
    toast(error.message, true);
  }
}

function resetFilters() {
  $("searchInput").value = "";
  $("groupFilter").value = "";
  $("statusFilter").value = "";
  loadEmails();
}

function openSettings() {
  $("clipboardClearSeconds").value = state.settings.clipboard_clear_seconds || "30";
  $("chatgptProxy").value = state.settings.chatgpt_proxy || "";
  $("settingsModal").showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    const result = await api("/api/settings", {
      method: "PUT",
      body: {
        clipboard_clear_seconds: $("clipboardClearSeconds").value,
        chatgpt_proxy: $("chatgptProxy").value.trim(),
      },
    });
    state.settings = result.settings;
    $("settingsModal").close();
    toast("设置已保存。");
  } catch (error) {
    toast(error.message, true);
  }
}

async function openGroups() {
  try {
    const data = await api("/api/groups");
    state.groups = data.groups || [];
    renderGroups();
    $("groupsModal").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderGroups() {
  const body = $("groupsBody");
  if (!body) return;
  body.innerHTML = (state.groups || []).map((group, index) => `
    <tr>
      <td>${escapeHtml(group.name)}</td>
      <td>${Number(group.email_count || 0)}</td>
      <td>${group.protected ? "内置" : "普通"}</td>
      <td>
        <div class="inline-actions">
          <button class="mini" data-group-action="view" data-index="${index}">查看</button>
          <button class="mini" data-group-action="rename" data-index="${index}" ${group.protected ? "disabled" : ""}>重命名</button>
          <button class="mini danger-light" data-group-action="delete" data-index="${index}" ${group.protected ? "disabled" : ""}>删除</button>
        </div>
      </td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-group-action]").forEach((button) => {
    button.addEventListener("click", handleGroupAction);
  });
}

async function createGroup(event) {
  event.preventDefault();
  const name = $("groupNameField").value.trim();
  if (!name) return;
  try {
    await api("/api/groups", { method: "POST", body: { name } });
    $("groupNameField").value = "";
    await loadEmails();
    toast("分组已新增。");
  } catch (error) {
    toast(error.message, true);
  }
}

async function handleGroupAction(event) {
  const button = event.currentTarget;
  const group = state.groups[Number(button.dataset.index)];
  if (!group) return;
  const action = button.dataset.groupAction;
  try {
    if (action === "view") {
      $("groupFilter").value = group.name;
      $("groupsModal").close();
      await loadEmails();
      return;
    }
    if (action === "rename") {
      const newName = prompt("输入新的分组名称：", group.name);
      if (newName === null) return;
      await api("/api/groups", {
        method: "PUT",
        body: { old_name: group.name, new_name: newName },
      });
      if ($("groupFilter").value === group.name) $("groupFilter").value = newName.trim();
      await loadEmails();
      toast("分组已重命名。");
      return;
    }
    if (action === "delete") {
      if (!confirm(`删除分组 ${group.name}？该组 ${group.email_count || 0} 个账号会移到未分组。`)) return;
      await api("/api/groups", { method: "DELETE", body: { name: group.name } });
      if ($("groupFilter").value === group.name) $("groupFilter").value = "";
      await loadEmails();
      toast("分组已删除。");
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function openLogs() {
  try {
    const data = await api("/api/logs");
    $("logsBody").innerHTML = (data.logs || []).map((log) => `
      <tr>
        <td>${escapeHtml(log.created_at)}</td>
        <td>${escapeHtml(log.action)}</td>
        <td>${escapeHtml(log.email || "")}</td>
        <td>${escapeHtml(log.detail || "")}</td>
      </tr>
    `).join("");
    $("logsModal").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

async function copyText(text, sensitive, message = "已复制。") {
  await navigator.clipboard.writeText(text || "");
  toast(message);
  const clearAfter = Number(state.settings.clipboard_clear_seconds || 0);
  if (sensitive && clearAfter > 0) {
    setTimeout(() => {
      navigator.clipboard.writeText("").catch(() => {});
    }, clearAfter * 1000);
  }
}

async function copyChatgptSessionUrl() {
  await copyText(CHATGPT_SESSION_URL, false);
  await api("/api/log", {
    method: "POST",
    body: { action: "copy_chatgpt_session_url", detail: CHATGPT_SESSION_URL },
  });
}

function monitorChatgptLogin(job) {
  if (!job?.id) return;
  state.chatgptLoginJobId = job.id;
  setChatgptLoginStatus(job);
  clearTimeout(state.chatgptLoginPollTimer);
  if (LOGIN_DONE_STATUSES.includes(job.status)) return;
  state.chatgptLoginPollTimer = setTimeout(pollChatgptLogin, 1800);
}

function monitorBulkChatgptLogin(job) {
  if (!job?.id) return;
  state.chatgptBulkLoginJobId = job.id;
  state.chatgptBulkLoginSavedCount = Number(job.saved_count || 0);
  setBulkChatgptLoginStatus(job);
  renderSelection();
  clearTimeout(state.chatgptBulkLoginPollTimer);
  if (LOGIN_DONE_STATUSES.includes(job.status)) {
    state.chatgptBulkLoginJobId = "";
    renderSelection();
    return;
  }
  state.chatgptBulkLoginPollTimer = setTimeout(pollBulkChatgptLogin, 1800);
}

async function pollChatgptLogin() {
  if (!state.chatgptLoginJobId) return;
  try {
    const result = await api(`/api/chatgpt-login-jobs/${state.chatgptLoginJobId}`);
    setChatgptLoginStatus(result.job);
    if (LOGIN_DONE_STATUSES.includes(result.job.status)) {
      const okMessage = result.job.clipboard_copied ? "session 已复制成功到剪贴板。" : result.job.message;
      if (result.job.status === "ok") await loadEmails();
      toast(result.job.status === "ok" ? okMessage : result.job.message, LOGIN_PROBLEM_STATUSES.includes(result.job.status));
      return;
    }
    state.chatgptLoginPollTimer = setTimeout(pollChatgptLogin, 1800);
  } catch (error) {
    if (error.message.includes("本地服务连接失败")) {
      setChatgptLoginStatus({ status: "running", message: "本地服务短暂无响应，正在重试..." });
      state.chatgptLoginPollTimer = setTimeout(pollChatgptLogin, 2500);
      return;
    }
    setChatgptLoginStatus({ status: "failed", message: error.message });
    toast(error.message, true);
  }
}

async function pollBulkChatgptLogin() {
  if (!state.chatgptBulkLoginJobId) return;
  try {
    const result = await api(`/api/chatgpt-bulk-login-jobs/${state.chatgptBulkLoginJobId}`);
    const job = result.job;
    setBulkChatgptLoginStatus(job);
    if (Number(job.saved_count || 0) !== state.chatgptBulkLoginSavedCount) {
      state.chatgptBulkLoginSavedCount = Number(job.saved_count || 0);
      await loadEmails();
    }
    if (LOGIN_DONE_STATUSES.includes(job.status)) {
      state.chatgptBulkLoginJobId = "";
      await loadEmails();
      renderSelection();
      toast(job.message, job.status === "failed");
      return;
    }
    state.chatgptBulkLoginPollTimer = setTimeout(pollBulkChatgptLogin, 1800);
  } catch (error) {
    if (error.message.includes("本地服务连接失败")) {
      setBulkChatgptLoginStatus({ status: "running", message: "本地服务短暂无响应，正在重试..." });
      state.chatgptBulkLoginPollTimer = setTimeout(pollBulkChatgptLogin, 2500);
      return;
    }
    state.chatgptBulkLoginJobId = "";
    renderSelection();
    setBulkChatgptLoginStatus({ status: "failed", message: error.message });
    toast(error.message, true);
  }
}

function setChatgptLoginStatus(job) {
  const statusNode = $("chatgptLoginStatus");
  const label = {
    pending: "GPT 登录：准备中",
    running: "GPT 登录：进行中",
    ok: "GPT 登录：session 已复制成功",
    failed: "GPT 登录：失败",
    closed: "GPT 登录：已结束",
    banned: "GPT 登录：被封",
    retry: "GPT 登录：重新尝试",
  }[job?.status] || "";
  const copiedText = job?.clipboard_copied ? " · 剪贴板已写入" : "";
  const savedText = job?.session_saved ? " · 已保存到账号" : "";
  const statusText = label ? `${label}${copiedText}${savedText} · ${job.message || ""}` : "";
  statusNode.textContent = statusText;
  statusNode.title = statusText;
  statusNode.classList.toggle("error-text", LOGIN_PROBLEM_STATUSES.includes(job?.status));
  statusNode.classList.toggle("ok-text", job?.status === "ok");
}

function setBulkChatgptLoginStatus(job) {
  const statusNode = $("chatgptLoginStatus");
  const total = Number(job?.total || 0);
  const completed = Number(job?.completed || 0);
  const running = Number(job?.running_count || 0);
  const concurrency = Number(job?.concurrency || 0);
  const saved = Number(job?.saved_count || 0);
  const failed = Number(job?.failed_count || 0);
  const current = job?.current_email ? ` · 当前：${job.current_email}` : "";
  const label = {
    pending: "批量 GPT 登录：准备中",
    running: `批量 GPT 登录：${completed}/${total} 完成 · 并行 ${running}/${concurrency} · 已保存 ${saved} · 失败 ${failed}${current}`,
    ok: `批量 GPT 登录：完成 · 已保存 ${saved}`,
    failed: `批量 GPT 登录：完成 · 已保存 ${saved} · 失败 ${failed}`,
    closed: "批量 GPT 登录：已结束",
    banned: `批量 GPT 登录：完成 · 已保存 ${saved} · 被封 ${failed}`,
    retry: `批量 GPT 登录：完成 · 已保存 ${saved} · 重新尝试 ${failed}`,
  }[job?.status] || "";
  const statusText = label ? `${label} · ${job.message || ""}` : "";
  statusNode.textContent = statusText;
  statusNode.title = statusText;
  statusNode.classList.toggle("error-text", LOGIN_PROBLEM_STATUSES.includes(job?.status));
  statusNode.classList.toggle("ok-text", job?.status === "ok");
}

async function api(path, options = {}) {
  const fetchOptions = {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
  };
  if (options.body !== undefined) {
    fetchOptions.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, fetchOptions);
  } catch (error) {
    throw new Error("本地服务连接失败，请确认服务仍在运行，或刷新页面后重试。");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || "请求失败。");
  }
  return data;
}

async function plainGet(path) {
  const response = await fetch(path);
  return response.json();
}

function rowsFromImport(text, fileName = "") {
  const normalized = text.replace(/^\uFEFF/, "");
  const lines = normalized
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length) return [];

  const firstLine = lines[0];
  const looksLikeDelimitedText =
    fileName.toLowerCase().endsWith(".txt") ||
    firstLine.includes("----") ||
    /^[^,\t|]+@[^,\t|]+[:|;]/.test(firstLine);

  if (looksLikeDelimitedText && !looksLikeHeaderLine(firstLine)) {
    return rowsFromDelimitedText(lines);
  }

  return rowsFromCsv(normalized);
}

function rowsFromDelimitedText(lines) {
  return lines.map((line) => {
    const parts = splitAccountLine(line);
    const email = parts[0] || "";
    const password = parts[1] || "";
    const oauthClientId = parts[2] || "";
    const oauthRefreshToken = parts.slice(3).join("----");
    const provider = inferProviderFromEmail(email);
    const item = {
      email,
      password,
      oauth_client_id: oauthClientId,
      oauth_refresh_token: oauthRefreshToken,
      provider,
      imap_host: provider === "Outlook" ? "outlook.office365.com" : "",
      imap_port: "993",
      group_name: provider || "其他",
      tags: "文本导入",
      remark: "",
    };
    if (parts.length > 2) {
      item.remark = "文本导入：OAuth 令牌已保存";
    }
    return item;
  });
}

function splitAccountLine(line) {
  if (line.includes("----")) return line.split("----").map((part) => part.trim());
  if (line.includes("\t")) return line.split("\t").map((part) => part.trim());
  if (line.includes("|")) return line.split("|").map((part) => part.trim());
  if (line.includes(";")) return line.split(";").map((part) => part.trim());
  const colonMatch = line.match(/^([^:]+@[^:]+):(.+)$/);
  if (colonMatch) return [colonMatch[1].trim(), colonMatch[2].trim()];
  return [line.trim(), ""];
}

function looksLikeHeaderLine(line) {
  const lowered = line.toLowerCase();
  return (
    lowered.includes("email") ||
    lowered.includes("password") ||
    line.includes("邮箱") ||
    line.includes("密码")
  );
}

function inferProviderFromEmail(email) {
  const domain = String(email).split("@")[1]?.toLowerCase() || "";
  if (["outlook.com", "hotmail.com", "live.com", "msn.com"].includes(domain)) return "Outlook";
  if (domain === "gmail.com") return "Gmail";
  if (domain === "qq.com") return "QQ";
  if (domain === "163.com") return "163";
  if (domain === "126.com") return "126";
  if (domain === "icloud.com" || domain === "me.com") return "iCloud";
  return "";
}

function rowsFromCsv(text) {
  const rows = parseCsv(text).filter((row) => row.some((cell) => String(cell).trim()));
  if (rows.length < 2) return [];
  const headers = rows[0].map((header) => normalizeHeader(header));
  return rows.slice(1).map((cells) => {
    const item = {};
    headers.forEach((header, index) => {
      if (header) item[header] = cells[index] || "";
    });
    return item;
  });
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];
    if (char === '"') {
      if (inQuotes && next === '"') {
        field += '"';
        index += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }
    if (char === "," && !inQuotes) {
      row.push(field.trim());
      field = "";
      continue;
    }
    if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && next === "\n") index += 1;
      row.push(field.trim());
      rows.push(row);
      row = [];
      field = "";
      continue;
    }
    field += char;
  }
  row.push(field.trim());
  rows.push(row);
  return rows;
}

function normalizeHeader(header) {
  const value = String(header || "").trim().toLowerCase().replace(/\s+/g, "");
  const aliases = {
    email: "email",
    邮箱: "email",
    邮箱账号: "email",
    账号: "email",
    password: "password",
    密码: "password",
    授权码: "password",
    provider: "provider",
    平台: "provider",
    imap_host: "imap_host",
    imap地址: "imap_host",
    imap服务器: "imap_host",
    imap_port: "imap_port",
    imap端口: "imap_port",
    group: "group_name",
    group_name: "group_name",
    分组: "group_name",
    tags: "tags",
    标签: "tags",
    remark: "remark",
    备注: "remark",
  };
  return aliases[value] || value;
}

function statusText(status) {
  return {
    unknown: "未检测",
    ok: "正常",
    failed: "失败",
    banned: "被封",
    retry: "重新尝试",
  }[status || "unknown"] || status;
}

function shortText(value, maxLength) {
  const text = String(value || "");
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}

function uniqueValues(values) {
  return [...new Set(values)].sort((a, b) => a.localeCompare(b, "zh-CN"));
}

function filenameFromDisposition(disposition, fallback) {
  const match = String(disposition || "").match(/filename="?([^"]+)"?/i);
  return match?.[1] || fallback;
}

function timestampForFile() {
  const pad = (value) => String(value).padStart(2, "0");
  const now = new Date();
  return [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
    "_",
    pad(now.getHours()),
    pad(now.getMinutes()),
    pad(now.getSeconds()),
  ].join("");
}

function debounce(fn, wait) {
  let timer = 0;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.style.background = isError ? "#8f2424" : "#1d2430";
  node.classList.remove("hidden");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.add("hidden"), 2600);
}
