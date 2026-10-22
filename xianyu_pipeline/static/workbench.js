/* Same-origin API client. Polling never loads form values or replaces task rows. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const views = { messages: "频道消息", download: "下载任务", upload: "上传任务", library: "素材库", history: "任务记录", channels: "来源频道", settings: "设置" };
  const downloadStates = {
    queued: ["等待下载", "gray"], downloading: ["下载中", ""], validating: ["校验中", "amber"],
    completed: ["已下载", ""], failed: ["下载失败", "red"], cancelled: ["已取消", "gray"], interrupted: ["下载中断", "red"]
  };
  const uploadStates = {
    waiting: ["待上传", "gray"], uploading: ["上传中", ""], link_pending: ["结果待确认 / 链接待获取", "amber"],
    completed: ["已完成", ""], local: ["本地模拟", "gray"], failed: ["上传失败", "red"],
    deleted: ["云端已删除", "gray"]
  };
  const retryable = new Set(["failed", "cancelled", "interrupted"]);
  const taskViews = new Set(["download", "upload", "library", "history"]);
  const actionLabels = { run: "执行流水线", download: "下载", retry: "重新下载", upload: "上传", "cloud-delete": "删除云端文件", "channel-scan": "获取频道消息", "telegram-channels-scan": "获取账号频道", "channel-download": "下载选中消息", "channel-cover": "加载高清图", "names/generate": "生成中文名称", "names/apply": "应用中文名称", "browser-check": "检查浏览器", "telegram-api-login": "Telegram API 登录", "quark-login": "夸克网页登录", "quark-cli-login": "夸克 CLI 登录", "quark-cli-check": "检查夸克 CLI", "quark-api-check": "检查夸克 API" };
  const pageActions = {
    messages: ["channel-scan", "channel-cover", "channel-download", "channel-topics-scan"],
    download: ["run", "download", "retry", "channel-download"],
    upload: ["run", "upload"],
    library: ["upload", "cloud-delete", "names/generate", "names/apply"],
    channels: ["telegram-channels-scan", "channel-topics-scan"],
    settings: ["browser-check", "telegram-api-login", "quark-login", "quark-cli-login", "quark-cli-check", "quark-api-check"]
  };
  const numberFields = ["quark_share_expire_days", "recent_message_limit", "channel_message_limit", "download_start_timeout_seconds", "download_transfer_timeout_seconds", "download_workers", "download_file_concurrency", "quark_cli_timeout_seconds", "quark_api_file_concurrency", "quark_api_timeout_seconds"];
  numberFields.push("source_topic_id");
  actionLabels["channel-topics-scan"] = "获取群组话题";
  const booleanFields = ["quark_headless", "exclude_forwarded_posts", "auto_upload_after_download"];
  const plainFields = ["source_channel_url", "telegram_api_proxy_url", "telegram_api_dc2_server", "inbox_dir", "browser_executable_path", "archive_test_executable_path", "upload_provider", "quark_folder_url", "quark_cli_mode", "quark_cli_executable_path", "quark_cli_wsl_distribution", "quark_cli_node_command", "quark_cli_script_path", "quark_cli_parent_fid", "quark_api_parent_fid", "rename_translation_provider", "rename_glossary"];
  plainFields.push("data_server_url", "data_server_workspace", "data_server_backend", "data_server_db_host",
    "data_server_db_name", "data_server_db_user", "data_server_db_tls", "data_server_db_ca");
  numberFields.push("data_server_interval_seconds", "data_server_db_port");
  booleanFields.push("data_server_enabled");
  const formFields = [...plainFields, ...numberFields, ...booleanFields, "telegram_api_id", "telegram_api_hash", "quark_cookie", "data_server_token", "data_server_db_password"];
  const savedHashMask = "********";
  const state = {
    view: "download", data: null, tasks: new Map(), rows: new Map(), selected: new Set(), active: null,
    images: new Map(), brokenImages: new Set(), filters: {}, errors: new Map(), config: null,
    configLoaded: false, apiConfigured: false, apiIdConfigured: false, proxyProtected: false, quarkConfigured: false, dataTokenConfigured: false, dataPasswordConfigured: false,
    configDirty: false, formRevision: 0, pending: false, logs: null, workbenchOK: false,
    lastSync: null, scroll: {}, browserRefreshPending: false, returnFocus: null,
    catalog: null, catalogRows: new Map(), catalogSelection: new Set(), scanRequested: false,
    previewReturnFocus: null, previewZoom: 1, previewMessageId: null, previewRequested: new Set(), previewErrors: new Map(),
    channels: null, joinedChannels: null, joinedScanRequested: false,
    expandedTopics: new Set(), topicScanRequested: "",
    pickerOpen: false, pickerFocusKey: "", failedAvatars: new Set(), channelRevision: 0,
    channelEditorMode: "add", channelEditorKey: "",
    namingRows: new Map(), namingDirty: new Set(), namingSelected: new Set()
  };
  for (const view of Object.keys(views)) state.filters[view] = { query: "", status: "all", chat: "" };
  let workbenchRequest = null;
  let logsRequest = null;
  let configRequest = null;
  let catalogRequest = null;
  let channelsRequest = null;
  let joinedChannelsRequest = null;
  let catalogTaskIndex = null;
  let pollTimer = null;
  let noticeTimer = null;

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  if (!$("channel-topic-bar")) {
    $("channel-messages-section").querySelector(".channel-toolbar").insertAdjacentHTML("beforebegin",
      '<div id="channel-topic-bar" class="channel-topic-bar" hidden><label for="channel-topic-select">话题分区</label><select id="channel-topic-select" aria-label="选择话题"></select><button id="refresh-channel-topics" type="button" class="icon-button" title="刷新话题列表" aria-label="刷新话题列表"><i data-lucide="refresh-cw"></i></button><span id="channel-topic-status" role="status"></span></div>');
  }
  if (!$("source_topic_id")) {
    $("source_channel_url").closest("label").insertAdjacentHTML("afterend",
      '<label id="source-topic-field" hidden>来源话题<select id="source_topic_id"><option value="0">请选择话题</option></select></label>');
  }
  // A running Python service may still hold the previous HTML template in memory.
  if (!$("channel-transfer-filter")) {
    const label = document.createElement("label");
    label.className = "channel-transfer-filter";
    label.innerHTML = icon("list-filter");
    const select = document.createElement("select");
    select.id = "channel-transfer-filter";
    select.setAttribute("aria-label", "筛选消息处理状态");
    for (const [value, title] of [["all", "全部状态"], ["new", "未下载"], ["downloaded", "已下载"], ["transferred", "已转存夸克"]]) {
      select.add(new Option(title, value));
    }
    label.append(select);
    $("channel-search").closest(".search-field").after(label);
  }
  if (!$("delete-cloud-task")) {
    const button = document.createElement("button");
    button.id = "delete-cloud-task";
    button.type = "button";
    button.className = "danger-button";
    button.hidden = true;
    button.innerHTML = `${icon("trash-2")}<span>删除云端文件</span>`;
    $("retry-link").after(button);
  }
  if (!$("remove-record-task")) {
    const button = document.createElement("button");
    button.id = "remove-record-task";
    button.type = "button";
    button.className = "danger-button";
    button.hidden = true;
    button.innerHTML = `${icon("list-x")}移除记录`;
    $("delete-cloud-task").after(button);
  }
  const text = (id, value) => { const element = $(id); const next = String(value ?? ""); if (element.textContent !== next) element.textContent = next; };
  const finite = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const deletionInfo = (task) => ({
    deleting: ["正在删除云端文件", "amber"], pending: ["云端删除待确认", "amber"],
    failed: ["云端删除失败", "red"], completed: ["云端已删除", "gray"]
  }[task.cloud_delete_status]);
  const deletionPending = (task) => ["deleting", "pending"].includes(task.cloud_delete_status);
  const statusInfo = (task, upload = false) => (upload ? deletionInfo(task) || uploadStates[task.upload_status] : downloadStates[task.download_status]) || ["状态未知", "gray"];
  const taskName = (task) => String(task.remote_filename || task.rename_target || task.title || task.filename || `素材 ${task.id}`);
  const providerName = (value) => ({ local: "本地模拟", quark_browser: "夸克网页", quark_cli: "夸克 CLI / WSL", quark_api: "夸克 API" }[value] || value || "未提供");
  const uploadPhase = (task) => ({ hashing: "计算文件哈希", checking: "核对网盘文件", uploading: "上传中", merging: "合并分片", renaming: "应用中文名称", sharing: "生成分享链接" }[task.upload_phase] || "");
  const validationName = (task) => task.validation === "passed" ? "已校验" : task.validation === "pending" ? "待校验" : "校验状态未知";
  const allTasks = () => [...state.tasks.values()];
  const channelName = (value) => String(value ?? "").replace(/^https?:\/\/t\.me\//i, "").replace(/^@/, "").replace(/\/.*$/, "").toLowerCase();
  const activeTask = () => state.tasks.get(state.active);
  const isLibraryItem = (task) => !task.record_removed_at
    && (task.download_status === "completed" || ["completed", "deleted"].includes(task.upload_status));
  const isUploadTask = (task) => task.download_status === "completed" || ["uploading", "link_pending", "completed", "local", "failed"].includes(task.upload_status);
  const canUpload = (task) => !task.remote_device && !deletionPending(task) && (task.upload_status === "link_pending" || (task.download_status === "completed"
    && (["waiting", "failed", "deleted"].includes(task.upload_status) || (task.upload_status === "local" && ["quark_browser", "quark_cli", "quark_api"].includes(state.config?.upload_provider)))));
  const canDeleteCloud = (task) => state.view === "library" && state.data?.cloud_delete_supported === true
    && task && isLibraryItem(task) && !task.remote_device && task.upload_provider === "quark_api"
    && task.upload_status === "completed" && Boolean(task.upload_remote_fid) && task.upload_remote_fid !== "0";
  const canRemoveRecord = (task) => state.view === "library" && state.data?.record_remove_supported === true
    && task && isLibraryItem(task) && !task.remote_device;
  const removalBlocked = (task) => deletionPending(task) || ["queued", "downloading", "validating"].includes(task.download_status)
    || ["uploading", "link_pending"].includes(task.upload_status);
  const deleteCloudLabel = (task) => task.cloud_delete_status === "pending" ? "重新确认云端删除"
    : task.cloud_delete_status === "failed" ? "重试删除云端文件" : "删除云端文件";
  const canRetry = (task) => !task.remote_device && retryable.has(task.download_status);
  const selectedUploadTasks = () => [...state.selected].map((id) => state.tasks.get(id)).filter((task) => task && canUpload(task));
  const isBusy = () => state.pending || !state.workbenchOK || Boolean(state.data?.running);
  const channelDirectoryBusy = () => state.pending || !state.workbenchOK
    || Boolean(state.data?.running && ["telegram-channels-scan", "channel-topics-scan"].includes(state.data.action));
  const taskBelongsToPage = () => state.data?.running
    && (state.view === "history" || pageActions[state.view]?.includes(state.data.action));
  const savedChannels = () => state.channels?.channels || [];
  const selectedChannel = () => savedChannels().find((row) => row.key === state.channels.active
    && row.key === channelName(state.config?.source_channel_url));
  const channelFor = (value) => savedChannels().find((row) => row.key === channelName(value));
  const channelLabel = (value) => channelFor(value)?.name || String(value || "未提供");
  const selectedTopic = () => (selectedChannel()?.topics || []).find((row) => row.topic_id === state.config?.source_topic_id
    && row.topic_id === state.channels?.active_topic_id);
  const selectedScope = () => selectedChannel() ? `${selectedChannel().key}${selectedChannel().forum && selectedTopic() ? `~${selectedTopic().topic_id}` : ""}` : "";
  const sourceTitle = () => [selectedChannel()?.name, selectedTopic()?.title].filter(Boolean).join(" / ");
  const canReadChannel = () => state.configLoaded && Boolean(selectedChannel())
    && (!selectedChannel().forum || Boolean(selectedTopic()));
  const canLoadChannelMedia = () => state.configLoaded && state.workbenchOK && !state.pending
    && canReadChannel()
    && (!state.data?.running || state.data.channel_requests_open === true)
    && !(state.data?.running && state.data.action === "channel-scan" && state.data.channel_scan?.stop_requested);

  function icons(root = document) {
    if (window.lucide) window.lucide.createIcons({ root, nameAttr: "data-lucide" });
  }

  function html(element, value) {
    if (element.dataset.markup === value) return;
    element.innerHTML = value;
    element.dataset.markup = value;
    icons(element);
  }

  function redact(value) {
    return String(value ?? "")
      .replace(/\b[0-9a-f]{32}\b/gi, "[凭证已隐藏]")
      .replace(/((?:api[_ -]?(?:id|hash)|password|cookie|authorization|session_string)\s*[=:]\s*)[^\s,;]+/gi, "$1[已隐藏]")
      .replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+@/gi, "$1[凭证已隐藏]@");
  }

  function safeURL(value) {
    if (typeof value !== "string" || !value.trim()) return "";
    try {
      const url = new URL(value, location.origin);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
    } catch { return ""; }
  }

  function externalURL(value) {
    return typeof value === "string" && /^https?:\/\//i.test(value) ? safeURL(value) : "";
  }

  function bytes(value) {
    if (!finite(value)) return "未知";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${value.toLocaleString("zh-CN", { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
  }

  function duration(value) {
    if (!finite(value)) return "剩余时间未知";
    const seconds = Math.ceil(value);
    if (seconds < 60) return `剩余 ${seconds} 秒`;
    if (seconds < 3600) return `剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    return `剩余 ${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
  }

  function updated(value) {
    if (!value) return "未提供";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
  }

  function setError(key, message, retry = "") {
    if (!message) state.errors.delete(key);
    else state.errors.set(key, { message: redact(message), retry });
    html($("errors"), [...state.errors.entries()].map(([id, error]) => `<div class="alert" role="alert">${icon("circle-alert")}<span>${escape(error.message)}</span>${error.retry ? `<button type="button" data-retry="${escape(error.retry)}">重试</button>` : id === "action" ? '<button type="button" data-dismiss-error="action" aria-label="关闭操作错误提示">关闭</button>' : ""}</div>`).join(""));
  }

  function notice(message) {
    clearTimeout(noticeTimer);
    text("notice", redact(message));
    $("notice").hidden = !message;
    if (message) noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 6500);
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options, signal: controller.signal });
      let payload;
      if (response.status !== 204) {
        try { payload = await response.json(); }
        catch { throw new Error(`${path}：${response.ok ? "返回了无效 JSON" : `HTTP ${response.status}，接口不可用`}`); }
      }
      if (!response.ok) throw new Error(`${path}：${payload?.message || payload?.error || `HTTP ${response.status}`}`);
      return payload ?? {};
    } catch (error) {
      if (error.name === "AbortError") throw new Error(`${path}：请求超时，请检查本地服务`);
      if (error instanceof TypeError) throw new Error(`${path}：无法连接本地服务`);
      throw error;
    } finally { clearTimeout(timer); }
  }

  const post = (path, body) => request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });

  function baseTasks(view = state.view) {
    if (view === "upload") return allTasks().filter(isUploadTask);
    if (view === "library") return allTasks().filter(isLibraryItem);
    return allTasks();
  }

  function matchesStatus(task, status) {
    if (status === "all") return true;
    if (state.view === "upload") {
      if (status === "waiting") return ["waiting", "local", "deleted"].includes(task.upload_status);
      if (status === "active") return task.upload_status === "uploading" || task.cloud_delete_status === "deleting";
      if (status === "done") return task.upload_status === "completed" && !deletionPending(task);
      return ["failed", "link_pending"].includes(task.upload_status) || ["pending", "failed"].includes(task.cloud_delete_status);
    }
    if (state.view === "library") {
      if (status === "pending") return task.validation !== "passed";
      if (status === "done") return task.validation === "passed";
      return task.upload_status !== "deleted" && !deletionPending(task) && Boolean(task.share_url);
    }
    if (status === "active") return ["queued", "downloading", "validating"].includes(task.download_status) || (state.view === "history" && task.upload_status === "uploading");
    if (status === "done") return task.download_status === "completed";
    return retryable.has(task.download_status) || (state.view === "history"
      && (["failed", "link_pending"].includes(task.upload_status) || ["pending", "failed"].includes(task.cloud_delete_status)));
  }

  function filteredTasks() {
    const filter = state.filters[state.view];
    const query = filter.query.trim().toLocaleLowerCase();
    return baseTasks().filter((task) => matchesStatus(task, filter.status)
      && (!filter.chat || channelName(task.chat) === filter.chat)
      && (!query || [task.title, task.filename, task.remote_filename, task.chinese_filename, task.chat, channelLabel(task.chat), task.id, task.message_id, task.local_path].some((value) => String(value ?? "").toLocaleLowerCase().includes(query))));
  }

  function progressMarkup(task, upload = false) {
    if (upload) {
      if (task.upload_status === "uploading") {
        const received = finite(task.upload_received) ? task.upload_received : 0;
        const total = finite(task.upload_size) && task.upload_size > 0 ? task.upload_size : 0;
        const known = total > 0;
        const percent = known ? Math.min(100, Math.max(0, received / total * 100)) : null;
        const preparing = ["hashing", "checking"].includes(task.upload_phase);
        const label = preparing ? uploadPhase(task) : known ? `${bytes(received)} / ${bytes(total)}` : "正在上传 / 等待网盘响应";
        const track = known
          ? `<div class="track" role="progressbar" aria-label="上传进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.floor(percent)}"><span style="width:${percent}%"></span></div>`
          : '<div class="track indeterminate" role="progressbar" aria-label="上传进度未知"><span></span></div>';
        const speed = finite(task.upload_speed) && task.upload_speed > 0 ? `${escape(bytes(task.upload_speed))}/s` : "速度未知";
        return `<div class="progress-label"><span>${escape(label)}</span>${known ? `<b>${Math.floor(percent)}%</b>` : ""}</div>${track}<div class="task-meta"><span>${escape(providerName(task.upload_provider))} · ${speed}</span><span>${escape(duration(task.upload_eta))}</span></div>`;
      }
      if (task.upload_status === "link_pending") return `<div class="task-meta"><span>${escape(state.data?.running && !task.error ? uploadPhase(task) || "结果待确认 / 链接待获取" : "结果待确认 / 链接待获取")}</span></div>`;
      return `<div class="task-meta"><span>${escape(bytes(task.size))} · ${escape(validationName(task))}</span><span>${escape(task.upload_status === "local" ? "本地模拟，未上传网盘" : statusInfo(task, true)[0])}</span></div>`;
    }
    const active = ["downloading", "validating"].includes(task.download_status);
    const known = finite(task.size) && task.size > 0 && finite(task.received);
    const percent = known ? Math.min(100, Math.max(0, task.received / task.size * 100)) : null;
    const receipt = finite(task.received) ? bytes(task.received) : "接收量未知";
    const size = finite(task.size) ? bytes(task.size) : "大小未知";
    let output = `<div class="progress-label"><span>${escape(finite(task.received) || active ? `${receipt} / ${size}` : size)}</span>${active && known ? `<b>${Math.floor(percent)}%</b>` : ""}</div>`;
    if (active) {
      const validating = task.download_status === "validating";
      output += `<div class="track ${validating ? "amber " : ""}${!known || validating ? "indeterminate" : ""}" role="progressbar" aria-label="${validating ? "正在校验，校验进度未知" : known ? "下载进度" : "下载进度未知"}"${known && !validating ? ` aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.floor(percent)}"` : ""}><span${known && !validating ? ` style="width:${percent}%"` : ""}></span></div>`;
      output += `<div class="task-meta"><span>${validating ? "正在校验文件" : finite(task.speed) ? `${escape(bytes(task.speed))}/s` : "速度未知"}</span><span>${validating ? "等待校验结果" : escape(duration(task.eta))}</span></div>`;
    } else {
      output += `<div class="task-meta"><span>${escape(task.download_status === "completed" ? validationName(task) : statusInfo(task)[0])}</span><span>${escape(task.images?.length ? `${task.images.length} 张图片` : "暂无图片")}</span></div>`;
    }
    return output;
  }

  function imageSlot(element, image, large = false) {
    const url = safeURL(image?.url);
    const broken = url && state.brokenImages.has(url);
    const key = JSON.stringify([url, image?.name, broken, large, image?.pending]);
    if (element.dataset.imageKey === key) return;
    element.dataset.imageKey = key;
    element.classList.add("image-slot");
    element.replaceChildren();
    if (!url || broken) {
      const fallback = document.createElement("span");
      fallback.className = "image-fallback";
      fallback.innerHTML = icon(image?.pending ? "loader-circle" : "file") + (large ? `<span>${broken ? "图片加载失败" : image?.url ? "图片地址不可用" : "暂无展示图"}</span>` : "");
      element.append(fallback);
      element.dataset.imageState = image?.pending ? "loading" : broken ? "failed" : "empty";
      icons(fallback);
      if (image?.pending) fallback.querySelector(".lucide")?.classList.add("loading-spinner");
      return;
    }
    const img = document.createElement("img");
    img.alt = String(image.name || "素材展示图");
    img.referrerPolicy = "no-referrer";
    img.loading = large ? "eager" : "lazy";
    img.decoding = "async";
    img.addEventListener("error", () => {
      state.brokenImages.add(url);
      imageSlot(element, image, large);
      setError("images", "部分素材图片加载失败，已显示文件图标。", "images");
    }, { once: true });
    img.src = url;
    element.dataset.imageState = "image";
    element.append(img);
  }

  function createRow(id) {
    const row = document.createElement("article");
    row.className = "task";
    row.dataset.taskId = id;
    row.innerHTML = `<input class="task-checkbox" type="checkbox"><button type="button" class="thumb-button" data-task-action="inspect"></button><div class="task-body"><div class="title-line"><button type="button" class="task-title" data-task-action="inspect"></button><span class="badge"></span></div><span class="badge row-cloud-badge" hidden></span><div class="task-sub"></div><div class="task-progress"></div><p class="error-note" hidden></p><button type="button" class="text-button row-retry" data-task-action="retry" hidden>${icon("rotate-cw")}重新下载</button><button type="button" class="text-button row-link" data-task-action="link" hidden>${icon("link")}重新获取链接</button><button type="button" class="text-button danger-button row-delete-cloud" data-task-action="delete-cloud" hidden>${icon("trash-2")}<span>删除云端文件</span></button><button type="button" class="text-button danger-button row-remove-record" data-task-action="remove-record" hidden>${icon("list-x")}移除记录</button></div><button type="button" class="task-tail" data-task-action="inspect" title="查看详情" aria-label="查看详情">${icon("chevron-right")}</button>`;
    icons(row);
    return row;
  }

  function updateRow(row, task) {
    const id = String(task.id);
    const upload = state.view === "upload" || (state.view === "history" && task.download_status === "completed")
      || (state.view === "library" && task.download_status !== "completed");
    const info = statusInfo(task, upload);
    row.classList.toggle("focused", state.active === id);
    row.classList.toggle("checked", state.selected.has(id));
    const checkbox = row.querySelector(".task-checkbox");
    checkbox.checked = state.selected.has(id);
    checkbox.disabled = Boolean(task.remote_device);
    checkbox.setAttribute("aria-label", `选择 ${taskName(task)}`);
    const title = row.querySelector(".task-title");
    title.textContent = taskName(task);
    title.setAttribute("aria-pressed", String(state.active === id));
    const badge = row.querySelector(".badge");
    badge.textContent = info[0];
    badge.className = `badge ${info[1]}`;
    const cloudBadge = row.querySelector(".row-cloud-badge");
    const cloudInfo = deletionInfo(task);
    cloudBadge.hidden = upload || !cloudInfo;
    cloudBadge.textContent = cloudInfo?.[0] || "";
    cloudBadge.className = `badge row-cloud-badge ${cloudInfo?.[1] || ""}`;
    row.querySelector(".task-sub").textContent = [task.filename, task.chat ? channelLabel(task.chat) : "", task.remote_device ? "其他设备" : ""].filter(Boolean).join(" · ") || "来源未提供";
    const thumb = row.querySelector(".thumb-button");
    thumb.setAttribute("aria-label", `查看 ${taskName(task)} 的详情`);
    imageSlot(thumb, task.images?.[0]);
    html(row.querySelector(".task-progress"), progressMarkup(task, upload));
    const error = row.querySelector(".error-note");
    error.hidden = !(task.cloud_delete_error || task.error || task.naming_error);
    error.textContent = redact(task.cloud_delete_error || task.error || task.naming_error);
    const retry = row.querySelector(".row-retry");
    retry.hidden = !canRetry(task);
    retry.disabled = isBusy();
    const link = row.querySelector(".row-link");
    link.hidden = task.remote_device || task.upload_status !== "link_pending";
    link.disabled = isBusy();
    const remove = row.querySelector(".row-delete-cloud");
    remove.hidden = !canDeleteCloud(task);
    remove.disabled = isBusy();
    remove.querySelector("span").textContent = deleteCloudLabel(task);
    const removeRecord = row.querySelector(".row-remove-record");
    removeRecord.hidden = !canRemoveRecord(task);
    removeRecord.disabled = isBusy() || removalBlocked(task);
    removeRecord.title = removalBlocked(task) ? "请先完成当前任务并确认云端结果" : "移除记录，保留本地文件和云端文件";
  }

  function renderFilters() {
    let tabs = [["all", "全部"], ["active", "进行中"], ["done", "已下载"], ["attention", "需处理"]];
    if (state.view === "upload") tabs = [["all", "全部"], ["waiting", "待上传"], ["active", "上传中"], ["done", "已完成"], ["attention", "待处理"]];
    if (state.view === "library") tabs = [["all", "全部"], ["done", "已校验"], ["pending", "待校验"], ["shared", "有分享链接"]];
    const filter = state.filters[state.view];
    const filterKey = tabs.map(([key]) => key).join(",");
    if ($("filters").dataset.filterKey !== `${state.view}:${filterKey}`) {
      html($("filters"), tabs.map(([key, label]) => `<button type="button" class="filter" data-filter="${key}" aria-pressed="false">${label}<small></small></button>`).join(""));
      $("filters").dataset.filterKey = `${state.view}:${filterKey}`;
    }
    for (const button of $("filters").children) {
      const selected = button.dataset.filter === filter.status;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
      button.querySelector("small").textContent = state.data ? baseTasks().filter((task) => matchesStatus(task, button.dataset.filter)).length : "—";
    }
    const chats = savedChannels();
    if (filter.chat && !chats.some((row) => row.key === filter.chat)) filter.chat = "";
    const options = '<option value="">全部频道</option>' + chats.map((row) => `<option value="${escape(row.key)}">${escape(row.name)}</option>`).join("");
    if ($("channel-filter").dataset.options !== options) {
      $("channel-filter").innerHTML = options;
      $("channel-filter").dataset.options = options;
    }
    $("channel-filter").value = filter.chat;
  }

  function renderList() {
    if (!taskViews.has(state.view)) return;
    renderFilters();
    const tasks = filteredTasks();
    if (state.active === null && tasks.length) state.active = String(tasks[0].id);
    const visible = new Set(tasks.map((task) => String(task.id)));
    for (const [id, row] of state.rows) {
      row.hidden = !visible.has(id);
      if (!state.tasks.has(id)) { row.remove(); state.rows.delete(id); }
    }
    for (const task of tasks) {
      const id = String(task.id);
      if (!state.rows.has(id)) {
        const row = createRow(id);
        state.rows.set(id, row);
        $("task-list").append(row);
      }
      const row = state.rows.get(id);
      row.hidden = false;
      updateRow(row, task);
    }
    const hasFilters = Boolean(state.filters[state.view].query || state.filters[state.view].chat || state.filters[state.view].status !== "all");
    $("empty-state").hidden = tasks.length > 0;
    $("clear-filters").hidden = !hasFilters;
    if (!state.data) {
      text("empty-title", state.errors.has("workbench") ? "暂时无法读取任务" : "正在读取任务");
      text("empty-description", state.errors.has("workbench") ? "工作台接口不可用，可重试连接或前往设置。" : "等待工作台响应");
    } else if (hasFilters) {
      text("empty-title", "没有匹配的素材"); text("empty-description", "当前搜索或筛选下没有任务。");
    } else {
      text("empty-title", ({ download: "还没有下载任务", upload: "还没有可上传的素材", library: "素材库为空", history: "还没有任务记录" })[state.view]);
      text("empty-description", ({ download: state.config?.source_channel_url ? "当前来源尚无任务记录。" : "尚未配置来源频道。", upload: "已下载的素材会显示在这里。", library: "暂无下载成功或上传成功的素材。", history: "运行日志显示在下方。" })[state.view]);
    }
    text("result-count", state.data ? `${tasks.length} / ${baseTasks().length} 个素材` : "—");
    text("queue-status", state.view === "upload" ? "待校验素材将在上传前校验" : state.view === "library" ? "已收录素材" : state.data?.running ? "任务运行中" : state.data ? "当前空闲" : "状态未知");
    renderSelection(tasks);
    renderInspector();
  }

  function renderSelection(tasks = taskViews.has(state.view) ? filteredTasks() : []) {
    tasks = tasks.filter((task) => !task.remote_device);
    const checked = tasks.filter((task) => state.selected.has(String(task.id))).length;
    $("select-all").checked = tasks.length > 0 && checked === tasks.length;
    $("select-all").indeterminate = checked > 0 && checked < tasks.length;
    $("select-all").disabled = !tasks.length;
    const eligible = selectedUploadTasks();
    text("selection-count", state.selected.size ? `已选 ${state.selected.size} · 可处理 ${eligible.length}` : "未选择");
    $("clear-selection").hidden = !state.selected.size;
    const linkOnly = eligible.length && eligible.every((task) => task.upload_status === "link_pending");
    $("upload").querySelector("span").textContent = linkOnly ? `获取选中链接 (${eligible.length})` : eligible.length ? `上传 / 处理选中 (${eligible.length})` : "上传选中素材";
    $("upload").disabled = isBusy() || !eligible.length;
  }

  function renderInspector() {
    const task = activeTask();
    $("detail-empty").hidden = Boolean(task);
    $("detail-content").hidden = !task;
    if (!task) {
      $("detail-empty").querySelector("p").textContent = state.active ? "该素材已不在当前任务数据中" : "尚未选择素材";
      return;
    }
    const images = Array.isArray(task.images) ? task.images : [];
    let index = state.images.get(state.active) || 0;
    if (index >= images.length) index = 0;
    state.images.set(state.active, index);
    imageSlot($("preview"), images[index], true);
    const galleryKey = JSON.stringify([state.active, images]);
    if ($("preview-strip").dataset.gallery !== galleryKey) {
      $("preview-strip").replaceChildren();
      images.forEach((image, imageIndex) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.imageIndex = imageIndex;
        button.title = image.name || `图片 ${imageIndex + 1}`;
        button.setAttribute("aria-label", `查看 ${button.title}`);
        imageSlot(button, image);
        $("preview-strip").append(button);
      });
      $("preview-strip").dataset.gallery = galleryKey;
    }
    for (const button of $("preview-strip").children) {
      const selected = Number(button.dataset.imageIndex) === index;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    }
    text("photo-info", images.length ? `${index + 1} / ${images.length} 张` : "0 张图片");
    text("selected-name", taskName(task)); text("selected-filename", task.filename || "文件名未提供");
    text("detail-chat", channelLabel(task.chat)); text("detail-message", task.message_id ?? "未提供");
    const imageSources = {preceding_album: "前置图集（自动关联）", preceding_photo: "前置单图（自动关联）",
      document_thumbnail: "附件缩略图", zip: "ZIP 内图片"};
    text("detail-image-source", images.length ? imageSources[task.image_source] || "来源未记录" : "暂无图片");
    text("detail-image-messages", Array.isArray(task.image_message_ids) && task.image_message_ids.length ? task.image_message_ids.join(", ") : "无");
    text("detail-size", bytes(task.size)); text("detail-download", statusInfo(task)[0]);
    text("detail-validation", validationName(task)); text("detail-upload", statusInfo(task, true)[0]);
    text("detail-path", task.remote_device ? "文件位于其他设备" : task.local_path || "暂无本地路径"); text("detail-provider", providerName(task.upload_provider));
    text("detail-updated", updated(task.updated_at));
    html($("detail-progress"), progressMarkup(task, state.view === "upload" || task.upload_status === "uploading"
      || task.upload_status === "link_pending" || state.view === "library" && task.download_status !== "completed"));
    text("detail-error", redact(task.cloud_delete_error || task.error || task.naming_error));
    $("detail-error").hidden = !(task.cloud_delete_error || task.error || task.naming_error);
    const source = externalURL(task.source_url);
    $("source-link").hidden = !source;
    if (source) $("source-link").href = source; else $("source-link").removeAttribute("href");
    const share = task.upload_status === "deleted" || deletionPending(task) ? "" : externalURL(task.share_url);
    $("share-result").hidden = !share;
    text("share-link", share);
    if (share) $("share-link").href = share; else $("share-link").removeAttribute("href");
    $("open-task").hidden = !task.local_path; $("open-task").disabled = state.pending || !state.workbenchOK;
    $("retry-task").hidden = !canRetry(task); $("retry-task").disabled = isBusy();
    $("retry-link").hidden = task.remote_device || task.upload_status !== "link_pending"; $("retry-link").disabled = isBusy();
    $("delete-cloud-task").hidden = !canDeleteCloud(task); $("delete-cloud-task").disabled = isBusy();
    $("delete-cloud-task").querySelector("span").textContent = deleteCloudLabel(task);
    $("remove-record-task").hidden = !canRemoveRecord(task);
    $("remove-record-task").disabled = isBusy() || removalBlocked(task);
    $("remove-record-task").title = removalBlocked(task) ? "请先完成当前任务并确认云端结果" : "移除记录，保留本地文件和云端文件";
  }

  function renderMetrics() {
    const tasks = allTasks();
    let metrics;
    if (state.view === "upload") metrics = [
      ["待上传素材", tasks.filter((task) => task.download_status === "completed" && ["waiting", "local", "deleted"].includes(task.upload_status)).length, "个已下载文件", ""],
      ["正在上传", tasks.filter((task) => task.upload_status === "uploading").length, "个任务", "green"],
      ["上传已完成", tasks.filter((task) => task.upload_status === "completed" && !deletionPending(task)).length, "个素材", ""],
      ["需要处理", tasks.filter((task) => ["failed", "link_pending"].includes(task.upload_status)
        || ["pending", "failed"].includes(task.cloud_delete_status)).length, "个任务", "amber"]
    ];
    else if (state.view === "library") metrics = [
      ["素材总数", baseTasks().length, "个文件", ""], ["已校验", baseTasks().filter((task) => task.validation === "passed").length, "个文件", "green"],
      ["待校验", baseTasks().filter((task) => task.validation !== "passed").length, "个文件", "amber"],
      ["有分享链接", baseTasks().filter((task) => task.upload_status !== "deleted" && !deletionPending(task) && externalURL(task.share_url)).length, "个素材", ""]
    ];
    else {
      const downloading = tasks.filter((task) => task.download_status === "downloading");
      const speedKnown = downloading.length > 0 && downloading.every((task) => finite(task.speed));
      const received = tasks.filter((task) => finite(task.received));
      metrics = [["任务总数", tasks.length, "个素材文件", ""],
        ["实时下载速度", speedKnown ? `${bytes(downloading.reduce((sum, task) => sum + task.speed, 0))}/s` : "—", "", "green"],
        ["已接收数据", received.length ? bytes(received.reduce((sum, task) => sum + task.received, 0)) : "—", received.length && received.length !== tasks.length ? "已知接收量" : "", ""],
        ["需要处理", tasks.filter((task) => retryable.has(task.download_status) || (state.view === "history"
          && (["failed", "link_pending"].includes(task.upload_status) || ["pending", "failed"].includes(task.cloud_delete_status)))).length, "个任务", "amber"]];
    }
    html($("metrics"), metrics.map(([label, value, unit, tone]) => `<div class="metric ${tone}"><div class="metric-label">${label}</div><div class="metric-value">${escape(state.data ? value : "—")}<small>${unit}</small></div></div>`).join(""));
  }

  function catalogChannel(value) {
    const key = channelName(value).trim();
    if (!key) return "";
    const channel = savedChannels().find((row) => row.key === key || channelName(row.username) === key
      || Number.isSafeInteger(row.id) && row.id > 0 && String(-(1000000000000 + row.id)) === key);
    return channel?.id > 0 ? String(-(1000000000000 + channel.id)) : key;
  }

  function catalogTasks(message) {
    if (!catalogTaskIndex) {
      catalogTaskIndex = new Map();
      for (const task of allTasks()) {
        const key = `${catalogChannel(task.chat)}:${Number(task.message_id)}`;
        if (!catalogTaskIndex.has(key)) catalogTaskIndex.set(key, []);
        catalogTaskIndex.get(key).push(task);
      }
    }
    return catalogTaskIndex.get(`${catalogChannel(message.chat)}:${message.message_id}`) || [];
  }

  function catalogTask(message) {
    const tasks = catalogTasks(message).filter((task) => !task.remote_device);
    return tasks.find((task) => task.download_status === "completed")
      || tasks.find((task) => ["queued", "downloading", "validating"].includes(task.download_status))
      || tasks[0];
  }

  function catalogTransfer(message) {
    const tasks = catalogTasks(message);
    const downloaded = tasks.find((task) => !task.remote_device && task.download_status === "completed")
      || tasks.find((task) => task.download_status === "completed");
    const cloudTasks = tasks.filter((task) => ["quark_api", "quark_cli", "quark_browser"].includes(task.upload_provider));
    const deleted = cloudTasks.find((task) => task.upload_status === "deleted");
    const deletedBindings = new Set(cloudTasks.filter((task) => task.upload_status === "deleted")
      .map((task) => `${task.upload_target}:${task.upload_remote_fid}`));
    const uploaded = cloudTasks.find((task) => task.upload_status === "completed" && !deletionPending(task)
      && !(task.upload_remote_fid && deletedBindings.has(`${task.upload_target}:${task.upload_remote_fid}`)));
    const deleting = cloudTasks.find(deletionPending);
    const uploading = cloudTasks.find((task) => ["uploading", "link_pending", "failed"].includes(task.upload_status));
    return { downloaded, uploaded, uploading, deleting, deleted, task: catalogTask(message) || uploaded || downloaded || tasks[0] };
  }

  function catalogBadges(message) {
    const transfer = catalogTransfer(message);
    const badges = [];
    const badge = (label, tone, symbol, title = label) =>
      `<span class="badge ${tone}" title="${escape(title)}">${icon(symbol)}${escape(label)}</span>`;
    if (transfer.downloaded) {
      const remote = transfer.downloaded.remote_device;
      badges.push(badge(remote ? "已下载 · 其他设备" : "已下载", "", "circle-check",
        remote ? "其他设备的已完成下载记录，本机可另行下载" : "本机的已完成下载记录"));
    }
    const local = catalogTask(message);
    if (!transfer.downloaded || local && local.download_status !== "completed") {
      const status = local ? statusInfo(local) : ["未下载", "gray"];
      badges.push(badge(status[0], status[1], "arrow-down-to-line"));
    }
    if (transfer.uploaded) {
      badges.push(badge("已转存夸克", "", "cloud-check", "夸克上传已完成，记录已确认"));
    } else if (transfer.deleting) {
      badges.push(badge("云端删除待确认", "amber", "cloud-alert"));
    } else if (transfer.deleted) {
      badges.push(badge("云端已删除", "gray", "cloud-off"));
    } else if (transfer.uploading) {
      const status = ({ uploading: ["转存中", ""], link_pending: ["转存待确认", "amber"],
        failed: ["转存失败", "red"] })[transfer.uploading.upload_status];
      badges.push(badge(status[0], status[1], "cloud-upload"));
    }
    return badges.join("");
  }

  function visibleChannelMessages() {
    if (!canReadChannel()) return [];
    const query = $("channel-search").value.trim().toLocaleLowerCase();
    const filter = $("channel-transfer-filter").value;
    return (state.catalog?.messages || []).filter((message) => {
      if (query && ![message.filename, message.text, message.message_id]
        .some((value) => String(value ?? "").toLocaleLowerCase().includes(query))) return false;
      const transfer = catalogTransfer(message);
      return filter === "all" || filter === "new" && !transfer.downloaded && !transfer.uploaded
        || filter === "downloaded" && Boolean(transfer.downloaded)
        || filter === "transferred" && Boolean(transfer.uploaded);
    });
  }

  function locallyDownloadableMessage(message) {
    const status = catalogTask(message)?.download_status;
    return !["completed", "queued", "downloading", "validating"].includes(status);
  }

  function downloadableMessage(message) {
    const transfer = catalogTransfer(message);
    return !transfer.downloaded && !transfer.uploaded && locallyDownloadableMessage(message);
  }

  function seriesLabel(message) {
    if (!message?.series) return "";
    const total = Number(message.series_total || 1);
    return `${message.series_name || "分卷系列"} · ${total} 个分卷`;
  }

  function renderChannelLoading() {
    const scan = canReadChannel() ? state.data?.channel_scan || {} : {};
    const active = Boolean(state.scanRequested || state.data?.running && state.data.action === "channel-scan");
    const stage = state.scanRequested ? "connecting" : scan.stage || "";
    const box = $("channel-loading");
    box.hidden = !stage;
    box.dataset.stage = stage;
    $("channel-messages-section").setAttribute("aria-busy", String(active));
    const target = scan.target || state.config?.channel_message_limit || 100;
    const found = Number(scan.found || 0);
    const processed = Number(scan.processed || 0);
    const total = Number(scan.total || found);
    const ready = Number(scan.ready || 0);
    const failed = Number(scan.failed || 0);
    const elapsed = Math.max(0, Math.floor(Number(scan.elapsed_seconds || 0)));
    const terminal = ["completed", "failed", "cancelled"].includes(stage);
    let title, detail;
    if (active && scan.stop_requested) {
      title = "正在停止加载"; detail = `已读取 ${found} 条消息 · 封面已处理 ${processed}/${total}`;
    } else if (stage === "connecting") {
      title = "正在连接 Telegram"; detail = `本轮目标 ${target} 条压缩包消息`;
    } else if (stage === "scanning") {
      title = `正在读取消息 · ${found}/${target}`;
      detail = `已检查 ${scan.scanned || 0} 条原始消息 · 封面已处理 ${processed}/${found}`;
    } else if (stage === "covers") {
      title = `消息已读取 · 正在加载封面 ${processed}/${total}`;
      detail = `本轮 ${found} 条消息 · ${ready} 张封面可用${failed ? ` · ${failed} 张失败` : ""}${scan.cached ? ` · ${scan.cached} 张缓存` : ""}`;
    } else if (stage === "completed") {
      title = `加载完成 · 本轮 ${found} 条消息`;
      detail = `${ready} 张封面可用${failed ? ` · ${failed} 张失败` : ""}${scan.no_cover ? ` · ${scan.no_cover} 条无配图` : ""}${found < target ? ` · 目标 ${target} 条` : ""}`;
    } else if (stage === "cancelled") {
      title = "加载已停止"; detail = `已读取 ${found} 条消息 · ${ready} 张封面可用`;
    } else if (stage === "failed") {
      title = "加载失败"; detail = `已读取 ${found} 条消息 · ${redact(state.data?.error || "请查看任务日志")}`;
    } else { title = ""; detail = ""; }
    text("channel-loading-title", title);
    text("channel-loading-detail", detail);
    text("channel-loading-time", `${terminal ? "耗时" : "已用时"} ${elapsed} 秒`);
    html($("channel-loading-icon"), icon(active ? "loader-circle" : stage === "completed" ? "circle-check" : stage === "failed" ? "circle-alert" : "circle-pause"));
    $("channel-loading-icon").querySelector(".lucide")?.classList.toggle("loading-spinner", active);
    const bar = $("channel-loading-progress");
    bar.hidden = terminal;
    if (stage === "covers" && total) { bar.max = total; bar.value = processed; }
    else if (stage === "scanning") { bar.max = target; bar.value = found; }
    else bar.removeAttribute("value");
    text("channel-scan-status", stage ? active ? "加载中" : title
      : state.catalog?.scanned_at ? `最近读取 ${updated(state.catalog.scanned_at)}` : "尚未读取频道");
    return active;
  }

  const prefetchedPictures = new Set();
  let prefetchTimer = null;
  let prefetchKey = "";
  const canPrefetchPictures = () => state.view === "messages" && !document.hidden
    && state.data?.running && state.data.channel_requests_open && !state.data.stop_requested
    && ["channel-scan", "channel-download"].includes(state.data.action)
    && !state.pending && !state.previewRequested.size && !$("channel-preview-dialog").open;

  function schedulePicturePrefetch() {
    const candidates = canPrefetchPictures() ? visibleChannelMessages().filter((m) => m.cover_url).slice(0, 3) : [];
    const candidate = candidates.find((m) => !m.full_cover_url && m.full_status !== "loading"
      && !prefetchedPictures.has(`${state.catalog?.channel}:${m.message_id}`));
    const key = candidate ? `${state.catalog?.channel}:${candidate.message_id}` : "";
    if (prefetchTimer && key === prefetchKey) return;
    clearTimeout(prefetchTimer);
    prefetchTimer = null;
    prefetchKey = key;
    if (!candidate) return;
    prefetchTimer = setTimeout(async () => {
      prefetchTimer = null;
      prefetchKey = "";
      if (!canPrefetchPictures()) return;
      prefetchedPictures.add(key);
      try { await post("/api/channel-cover", { id: candidate.message_id, prefetch: true }); } catch (_) { /* Click remains available for retry. */ }
    }, 1500);
  }

  function renderCatalog() {
    if (state.view !== "messages") return;
    const rows = visibleChannelMessages();
    const canAct = canLoadChannelMedia();
    const canScan = !isBusy() && canReadChannel();
    const canSelect = state.workbenchOK && canReadChannel() && !state.pending;
    const loading = renderChannelLoading();
    const selected = (state.catalog?.messages || []).filter((message) => state.catalogSelection.has(message.message_id) && downloadableMessage(message));
    for (const id of state.catalogSelection) {
      const message = state.catalog?.messages.find((item) => item.message_id === id);
      if (!message || !downloadableMessage(message)) state.catalogSelection.delete(id);
    }
    text("channel-selected-count", state.catalogSelection.size ? `已选择 ${state.catalogSelection.size} 条` : "未选择");
    const selectable = rows.filter(downloadableMessage);
    $("channel-select-all").checked = selectable.length > 0 && selectable.every((message) => state.catalogSelection.has(message.message_id));
    $("channel-select-all").disabled = !canSelect || !selectable.length;
    $("channel-download-selected").disabled = !canAct || !selected.length;
    $("channel-load-older").hidden = !state.catalog?.has_more || !state.catalog?.oldest_scanned_id;
    $("channel-load-older").disabled = !canScan;
    const transfers = (state.catalog?.messages || []).map(catalogTransfer);
    text("channel-result-count", state.catalog ? `${rows.length} / ${state.catalog.messages.length} 条压缩包消息 · 已下载 ${transfers.filter((item) => item.downloaded).length} · 已转存 ${transfers.filter((item) => item.uploaded).length}` : "等待频道消息");
    const showEmpty = !rows.length;
    $("channel-empty").hidden = !showEmpty;
    $("channel-empty-action").hidden = loading || !canReadChannel() || Boolean(state.catalog?.messages.length);
    $("channel-empty-action").disabled = !canScan;
    if (showEmpty) {
      text("channel-empty-title", !savedChannels().length ? "尚未保存频道"
        : selectedChannel()?.forum && !selectedTopic() ? "请选择话题分区"
        : !canReadChannel() ? "尚未选择频道"
        : state.catalog?.messages.length ? "没有匹配的消息" : loading ? "正在读取消息" : state.catalog?.scanned_at ? "未找到 ZIP/RAR 消息" : "尚未获取频道消息");
      text("channel-empty-detail", state.catalog?.messages.length ? "试试其他文件名、消息 ID 或处理状态。" : "");
    }
    const list = $("channel-message-list");
    const visible = new Set(rows.map((message) => message.message_id));
    for (const [id, row] of state.catalogRows) {
      if (!visible.has(id)) { row.remove(); state.catalogRows.delete(id); }
    }
    rows.forEach((message, index) => {
      const id = message.message_id;
      let row = state.catalogRows.get(id);
      if (!row) {
        row = document.createElement("article");
        row.className = "channel-message";
        row.dataset.messageId = id;
        row.innerHTML = '<input type="checkbox" class="channel-checkbox"><button type="button" class="channel-message-cover" data-channel-action="preview"></button><div class="channel-message-body"></div>';
        state.catalogRows.set(id, row);
      }
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
      const checkbox = row.querySelector(".channel-checkbox");
      checkbox.setAttribute("aria-label", `选择 ${message.filename}`);
      checkbox.checked = state.catalogSelection.has(id);
      checkbox.disabled = !canSelect || !downloadableMessage(message);
      const cover = row.querySelector(".channel-message-cover");
      const pending = loading && message.cover_status === "pending" && !message.cover_url;
      cover.disabled = !message.cover_url && !(message.cover_status === "failed" && canAct);
      cover.title = message.cover_url ? "放大图片" : pending ? "封面加载中" : message.cover_status === "failed" ? "重新加载图片" : "暂无配图";
      cover.setAttribute("aria-label", `${message.filename}：${cover.title}`);
      imageSlot(cover, { url: message.cover_url || "", name: message.filename, pending });
      const transfer = catalogTransfer(message);
      const task = transfer.task;
      const share = externalURL(transfer.uploaded?.share_url);
      const downloadLabel = (transfer.downloaded || transfer.uploaded) && locallyDownloadableMessage(message)
        ? "下载到本机" : "下载";
      const source = externalURL(message.source_url);
      const caption = String(message.text || "").trim().slice(0, 280);
      html(row.querySelector(".channel-message-body"), `
          <div class="channel-message-head"><h2>${escape(message.filename)}</h2><div class="channel-message-badges">${catalogBadges(message)}</div></div>
          <div class="channel-message-meta"><span>消息 #${id}</span><span>${escape(bytes(message.size))}</span><time>${escape(updated(message.date))}</time>${message.series ? `<span class="cover-quality">${escape(seriesLabel(message))}</span>` : ""}${message.cover_url ? `<span class="cover-quality">${message.cover_quality === "full" ? "高清图" : message.cover_quality === "preview" ? "预览图" : "缩略图"}</span>` : ""}</div>
          ${caption ? `<p class="channel-caption">${escape(caption)}</p>` : ""}
          <div class="channel-message-actions">
            <button type="button" data-channel-action="download" ${!canAct || !locallyDownloadableMessage(message) ? "disabled" : ""}>${icon("arrow-down-to-line")}${downloadLabel}</button>
            ${share ? `<a class="button" href="${escape(share)}" target="_blank" rel="noopener noreferrer">${icon("cloud")}查看夸克</a>` : ""}
            ${source ? `<a class="button" href="${escape(source)}" target="_blank" rel="noopener noreferrer">${icon("external-link")}原消息</a>` : ""}
            ${task ? `<button type="button" class="text-button" data-channel-action="task">${icon("list-checks")}任务记录</button>` : ""}
          </div>
      `);
    });
    renderChannelPreview();
    schedulePicturePrefetch();
  }

  function renderChannelSwitcher() {
    const rows = savedChannels();
    const current = selectedChannel();
    const select = $("channel-select");
    select.value = current?.key || "";
    select.disabled = isBusy() || !rows.length;
    text("channel-selected-title", current?.name || (rows.length ? "选择频道" : "尚未保存频道"));
    text("channel-active-hint", current ? channelSubtitle(current) : rows.length ? `${rows.length} 个已保存频道` : "");
    html($("channel-selected-avatar"), avatarMarkup(current));
    renderTopicSwitcher();
    if (select.disabled && state.pickerOpen) closeChannelPicker(false);
    renderChannelOptions();
    const joined = state.joinedChannels;
    const scanning = state.joinedScanRequested || state.data?.running && state.data.action === "telegram-channels-scan";
    const status = $("joined-channel-status");
    status.dataset.stage = joined?.stage || "idle";
    text("joined-channel-status", scanning ? "正在获取账号频道"
      : joined?.stage === "failed" ? `获取失败：${redact(joined.error || "请重试")}；已保存频道保留`
      : joined?.stage === "cancelled" ? "获取已停止，已保存频道保留"
      : joined?.stage === "completed" ? `获取 ${joined.found || 0} 个 · 新增 ${joined.added || 0} 个 · 更新 ${joined.updated || 0} 个${joined.scanned_at ? ` · ${updated(joined.scanned_at)}` : ""}`
      : "");
    $("scan-joined-channels").disabled = isBusy() || !state.configLoaded || scanning;
    $("scan-joined-channels").title = state.data?.running && !scanning
      ? `正在${actionLabels[state.data.action] || "执行其他任务"}，结束后可获取账号频道` : "";
    html($("scan-joined-channels"), `${icon(scanning ? "loader-circle" : "radio-tower")}${scanning ? "获取中" : joined?.stage === "completed" ? "重新获取频道" : "获取账号频道"}`);
    $("scan-joined-channels").querySelector(".lucide")?.classList.toggle("loading-spinner", Boolean(scanning));
  }

  function channelSubtitle(row) {
    const kind = row.forum ? "话题群组" : row.kind === "supergroup" ? "超级群组" : "频道";
    return row.username ? `${kind} · @${row.username}` : `私有${kind} · ${row.key}`;
  }

  function topicScanLabel(key) {
    const scan = state.data?.telegram_topics;
    if (state.topicScanRequested === key || scan?.channel === key && scan.stage === "loading") return "正在获取话题";
    if (scan?.channel === key && scan.stage === "failed") return `获取失败：${redact(scan.error || "请重试")}；原列表保留`;
    if (scan?.channel === key && scan.stage === "cancelled") return "获取已停止，原列表保留";
    const row = channelFor(key);
    return row?.topics_scanned_at ? `${row.topics?.length || 0} 个话题 · ${updated(row.topics_scanned_at)}` : "尚未获取话题";
  }

  function topicOptions(row) {
    return '<option value="0">请选择话题</option>' + (row?.topics || []).map((topic) =>
      `<option value="${topic.topic_id}">${escape(topic.title)}${topic.closed ? " · 已关闭" : ""}${topic.hidden ? " · 已隐藏" : ""}</option>`).join("");
  }

  function renderTopicSwitcher() {
    const row = selectedChannel();
    $("channel-topic-bar").hidden = !row?.forum;
    if (!row?.forum) return;
    const select = $("channel-topic-select");
    html(select, topicOptions(row));
    select.value = String(selectedTopic()?.topic_id || 0);
    select.disabled = isBusy();
    $("refresh-channel-topics").disabled = isBusy();
    text("channel-topic-status", topicScanLabel(row.key));
  }

  function avatarMarkup(row) {
    const initial = Array.from(String(row?.name || row?.title || row?.username || "").trim())[0] || "";
    const url = String(row?.avatar_url || "");
    const source = /^\/api\/channel-avatar\/[0-9]+(?:\?[^#]*)?$/.test(url) && !state.failedAvatars.has(url) ? url : "";
    return `<span aria-hidden="true">${initial ? escape(initial.toLocaleUpperCase()) : icon("radio-tower")}</span>${source ? `<img src="${escape(source)}" alt="" loading="lazy">` : ""}`;
  }

  function filteredChannelOptions() {
    const query = $("channel-picker-search").value.trim().toLocaleLowerCase();
    return savedChannels().filter((row) => !query
      || [row.name, row.title, row.username, row.key].some((value) => String(value ?? "").toLocaleLowerCase().includes(query)));
  }

  function renderChannelOptions() {
    if (!state.pickerOpen) return;
    const rows = filteredChannelOptions();
    const selected = selectedChannel()?.key;
    html($("channel-options"), rows.map((row) => `<div id="channel-option-${escape(row.key)}" class="channel-option" role="option" aria-selected="${row.key === selected}" title="${escape(row.name || row.title)}" data-channel-option data-channel-key="${escape(row.key)}">
      <span class="channel-avatar avatar-tone-${Number(row.id || 0) % 4}" aria-hidden="true">${avatarMarkup(row)}</span>
      <span class="channel-option-copy"><strong>${escape(row.name || row.title || row.key)}</strong><small>${escape(channelSubtitle(row))}${row.verified ? " · 已认证" : ""}</small></span>
      ${row.key === selected ? icon("check") : ""}
    </div>`).join(""));
    $("channel-picker-empty").hidden = Boolean(rows.length);
    if (!rows.some((row) => row.key === state.pickerFocusKey)) state.pickerFocusKey = rows[0]?.key || "";
    highlightChannelOption();
  }

  function highlightChannelOption(scroll = false) {
    let focused = null;
    for (const option of $("channel-options").children) {
      const active = option.dataset.channelKey === state.pickerFocusKey;
      option.classList.toggle("picker-focused", active);
      if (active) focused = option;
    }
    const search = $("channel-picker-search");
    if (focused) search.setAttribute("aria-activedescendant", focused.id);
    else search.removeAttribute("aria-activedescendant");
    if (scroll && focused) focused.scrollIntoView({ block: "nearest" });
  }

  function openChannelPicker() {
    if ($("channel-select").disabled) return;
    state.pickerOpen = true;
    $("channel-picker-search").value = "";
    state.pickerFocusKey = selectedChannel()?.key || savedChannels()[0]?.key || "";
    $("channel-picker-panel").hidden = false;
    $("channel-select").setAttribute("aria-expanded", "true");
    renderChannelOptions();
    highlightChannelOption(true);
    $("channel-picker-search").focus({ preventScroll: true });
  }

  function closeChannelPicker(focus = true) {
    state.pickerOpen = false;
    $("channel-picker-panel").hidden = true;
    $("channel-select").setAttribute("aria-expanded", "false");
    $("channel-picker-search").removeAttribute("aria-activedescendant");
    if (focus) $("channel-select").focus({ preventScroll: true });
  }

  function updatePreviewZoom() {
    const image = $("channel-preview-image");
    const zoomed = state.previewZoom > 1;
    image.style.width = zoomed ? `${state.previewZoom * 100}%` : "";
    image.style.maxWidth = zoomed ? "none" : "";
    image.style.maxHeight = zoomed ? "none" : "";
    text("channel-zoom-level", `${Math.round(state.previewZoom * 100)}%`);
    $("channel-zoom-out").disabled = !zoomed;
    $("channel-zoom-in").disabled = state.previewZoom >= 3;
  }

  function openChannelPreview(message, trigger) {
    state.previewReturnFocus = trigger;
    state.previewZoom = 1;
    state.previewMessageId = message.message_id;
    text("channel-preview-title", message.filename);
    const image = $("channel-preview-image");
    image.alt = `频道消息 ${message.message_id} 的封面`;
    const url = safeURL(message.full_cover_url || message.cover_url);
    if (url) image.src = url; else image.removeAttribute("src");
    updatePreviewZoom();
    $("channel-preview-dialog").showModal();
    $("channel-preview-close").focus();
    renderChannelPreview();
    if (!message.full_cover_url) void loadChannelFullImage(message.message_id);
  }

  function renderChannelPreview() {
    if (!$("channel-preview-dialog").open) return;
    const message = state.catalog?.messages.find((row) => row.message_id === state.previewMessageId);
    if (!message) return;
    const full = safeURL(message.full_cover_url);
    if (full && $("channel-preview-image").src !== full) $("channel-preview-image").src = full;
    const loading = !full && (message.full_status === "loading" || state.previewRequested.has(message.message_id));
    const error = state.previewErrors.get(message.message_id) || message.full_error;
    const failed = !full && !loading && (Boolean(error) || ["failed", "cancelled"].includes(message.full_status));
    text("channel-preview-status-text", full
      ? message.full_quality === "document_thumbnail" ? "附件预览图" : message.full_quality === "large" ? "清晰大图（非最大尺寸）" : "最大尺寸图"
      : loading ? "正在加载高清图" : failed ? `高清图未加载：${redact(error || "请重试")}` : "预览图");
    html($("channel-preview-status-icon"), icon(loading ? "loader-circle" : failed ? "circle-alert" : full ? "image" : "eye"));
    $("channel-preview-status-icon").querySelector(".lucide")?.classList.toggle("loading-spinner", loading);
    $("channel-preview-retry").hidden = !failed;
    $("channel-preview-retry").disabled = !canLoadChannelMedia();
    $("channel-preview-status").dataset.stage = loading ? "loading" : failed ? "failed" : "ready";
  }

  async function loadChannelFullImage(id, original = false) {
    if (!canLoadChannelMedia() || state.previewRequested.has(id)) return;
    state.previewErrors.delete(id);
    state.previewRequested.add(id);
    renderChannelPreview();
    try {
      const result = await post("/api/channel-cover", { id, original });
      if (result.url && state.previewMessageId === id && $("channel-preview-dialog").open) {
        $("channel-preview-image").src = safeURL(result.url);
      }
      await refreshAll();
    } catch (error) {
      state.previewErrors.set(id, error.message);
    } finally {
      state.previewRequested.delete(id);
      renderChannelPreview();
    }
  }

  function renderHeader() {
    const data = state.data;
    const source = state.config?.source_channel_url;
    const subtitle = ({ messages: sourceTitle() || "已保存频道", download: source ? sourceTitle() || channelLabel(source) : "尚未配置来源频道", upload: data?.upload_target || "上传目标尚未读取", library: "已下载或已上传的素材", history: "任务状态与运行日志", channels: `${savedChannels().length} 个已保存频道`, settings: "本地配置" })[state.view];
    text("page-subtitle", state.configLoaded || state.view !== "download" ? subtitle : "来源配置尚未读取");
    text("download-source", source ? sourceTitle() || channelLabel(source) : state.configLoaded ? "尚未配置来源频道" : "来源配置尚未读取");
    text("upload-target", data?.upload_target || "未配置");
    const runningLabel = actionLabels[data?.action] || "执行任务";
    const connectionLabel = !state.workbenchOK ? state.errors.has("workbench") ? "接口连接失败" : "正在读取状态"
      : data.running ? `${taskBelongsToPage() ? "正在" : "后台正在"}${runningLabel}` : "工作台可用";
    $("connection").querySelector("span:last-child").textContent = connectionLabel;
    $("connection").querySelector(".status-dot").className = `status-dot ${!state.workbenchOK ? state.errors.has("workbench") ? "error" : "unknown" : data.running ? "busy" : ""}`;
    $("connection").title = connectionLabel;
    $("connection").setAttribute("aria-label", connectionLabel);
    $("download").hidden = state.view !== "download";
    $("download").disabled = isBusy() || !state.configLoaded || Boolean(source && !canReadChannel());
    $("scan-channel").hidden = state.view !== "messages";
    $("scan-channel").disabled = isBusy() || !canReadChannel();
    const scanning = state.scanRequested || data?.running && data.action === "channel-scan";
    html($("scan-channel"), `${icon(scanning ? "loader-circle" : "messages-square")}${scanning ? "获取中" : "获取频道消息"}`);
    $("scan-channel").querySelector(".lucide")?.classList.toggle("loading-spinner", Boolean(scanning));
    $("stop").hidden = !taskBelongsToPage();
    $("stop").disabled = state.pending || !state.workbenchOK || !data?.task_id || Boolean(data?.stop_requested);
    $("stop").dataset.taskId = data?.task_id || "";
    $("stop").dataset.action = data?.action || "";
    html($("stop"), `${icon("square")}${data?.stop_requested ? "正在停止" : `停止${runningLabel}`}`);
    $("stop").title = data?.running && !data.task_id ? "请重启本地程序后再停止任务" : `停止${runningLabel}`;
    $("open-downloads").hidden = !["download", "library", "settings"].includes(state.view);
    $("open-downloads").disabled = state.pending || (!data && !state.configLoaded);
    const cloudURL = externalURL(data?.upload_target);
    $("open-cloud").hidden = state.view !== "upload" || !cloudURL;
    if (cloudURL) $("open-cloud").href = cloudURL; else $("open-cloud").removeAttribute("href");
    $("upload").hidden = !["upload", "library"].includes(state.view);
    $("open-naming").hidden = !["upload", "library"].includes(state.view);
    $("open-naming").disabled = isBusy() || !allTasks().some((task) => task.download_status === "completed" && !task.remote_device);
    $("edit-channel").hidden = state.view !== "channels";
    $("add-channel").hidden = state.view !== "channels";
    $("add-channel").disabled = channelDirectoryBusy();
    $("channel-add-section").disabled = channelDirectoryBusy();
    $("destination").hidden = state.view !== "upload";
    $("download-settings").hidden = state.view !== "download";
    for (const id of ["save-config", "telegram-login", "quark-login", "quark-cli-login", "quark-cli-check", "quark-api-check", "browser-check"]) $(id).disabled = isBusy() || !state.configLoaded;
    $("quark-api-check").hidden = $("upload_provider").value !== "quark_api";
    $("quark-login").hidden = $("upload_provider").value !== "quark_browser";
    $("quark-cli-login").hidden = $("upload_provider").value !== "quark_cli";
    $("quark-cli-check").hidden = $("upload_provider").value !== "quark_cli";
    $("config-fields").disabled = !state.configLoaded || state.pending;
    text("nav-download", data ? state.tasks.size : "—");
    text("nav-messages", state.catalog ? state.catalog.messages.length : "—");
    text("nav-upload", data ? allTasks().filter(isUploadTask).length : "—");
    text("nav-library", data ? allTasks().filter(isLibraryItem).length : "—");
    const disk = data?.disk;
    const diskKnown = disk && finite(disk.free) && finite(disk.total) && disk.total > 0 && disk.free <= disk.total;
    $("disk-track").hidden = !diskKnown;
    if (diskKnown) {
      const used = (disk.total - disk.free) / disk.total * 100;
      text("disk-percent", `${Math.round(used)}%`);
      $("disk-track").firstElementChild.style.width = `${used}%`;
    } else text("disk-percent", "—");
    text("disk-space", disk && finite(disk.free) ? `可用空间 ${bytes(disk.free)}` : "存储信息不可用");
    text("access-mode", state.configLoaded ? "Telegram API 模式" : "获取方式未读取");
    text("footer-context", state.configLoaded ? "本地工作空间 / Telegram API" : "本地工作空间");
    text("last-sync", state.lastSync ? `${state.workbenchOK ? "最近同步" : "上次成功同步"} ${state.lastSync.toLocaleTimeString("zh-CN", { hour12: false })}` : "尚未同步");
    renderChannelSwitcher();
    renderSourceOptions();
    renderDataSync();
    renderSelection();
    renderCatalog();
  }

  function renderChannels() {
    const active = selectedChannel()?.key;
    const saved = savedChannels();
    text("saved-channel-count", saved.length);
    html($("saved-channel-list"), saved.length ? saved.map((row) => {
      const current = row.key === active;
      const link = externalURL(row.url);
      const count = allTasks().filter((task) => channelName(task.chat) === row.key).length;
      const expanded = state.expandedTopics.has(row.key);
      return `<div class="channel-row saved-channel-row${current ? " active" : ""}">
        <div class="saved-channel-info">${row.forum ? `<button class="icon-button topic-expand" type="button" data-channel-management="topics" data-channel-key="${escape(row.key)}" aria-label="${expanded ? "收起" : "展开"} ${escape(row.name)} 的话题" aria-expanded="${expanded}" title="${expanded ? "收起" : "展开"}话题">${icon(expanded ? "chevron-down" : "chevron-right")}</button>` : ""}<span class="channel-avatar avatar-tone-${Number(row.id || 0) % 4}" aria-hidden="true">${avatarMarkup(row)}</span><div><h3>${escape(row.name)}${current ? ' <span class="badge">当前</span>' : ""}</h3><p>${escape(channelSubtitle(row))} · ${count} 个素材</p></div></div>
        <div class="channel-actions">
          ${current ? `<button type="button" disabled>${icon("check")}当前频道</button>` : `<button type="button" data-channel-management="select" data-channel-key="${escape(row.key)}" ${isBusy() ? "disabled" : ""}>${icon("radio-tower")}切换</button>`}
          ${count ? `<button type="button" data-channel="${escape(row.key)}">${icon("list-checks")}任务</button>` : ""}
          ${link ? `<a class="button" href="${escape(link)}" target="_blank" rel="noopener noreferrer">${icon("external-link")}打开</a>` : ""}
          <button type="button" data-channel-management="edit" data-channel-key="${escape(row.key)}" ${channelDirectoryBusy() ? "disabled" : ""}>${icon("pencil")}编辑</button>
          <button type="button" data-channel-management="remove" data-channel-key="${escape(row.key)}" ${current || channelDirectoryBusy() ? "disabled" : ""}>${icon("trash-2")}删除</button>
        </div>
      </div>${row.forum && expanded ? `<div class="topic-children" data-topic-parent="${escape(row.key)}">
        <div class="topic-toolbar"><button type="button" data-channel-management="refresh-topics" data-channel-key="${escape(row.key)}" ${isBusy() ? "disabled" : ""}>${icon("refresh-cw")}刷新话题</button><span class="channel-topic-status" role="status">${escape(topicScanLabel(row.key))}</span></div>
        ${(row.topics || []).map((topic) => {
          const selected = current && selectedTopic()?.topic_id === topic.topic_id;
          return `<div class="topic-row${selected ? " active" : ""}" data-topic-id="${topic.topic_id}">
            <div class="topic-copy">${icon("hash")}<strong>${escape(topic.title)}</strong><small>#${topic.topic_id}${topic.closed ? " · 已关闭" : ""}${topic.hidden ? " · 已隐藏" : ""}</small></div>
            <button type="button" data-channel-management="select" data-channel-key="${escape(row.key)}" data-topic-id="${topic.topic_id}" ${selected || isBusy() ? "disabled" : ""}>${icon(selected ? "check" : "arrow-right")}${selected ? "当前话题" : "选择"}</button>
          </div>`;
        }).join("")}
      </div>` : ""}`;
    }).join("") : '<div class="empty-state compact"><p>暂无已保存频道</p></div>');
  }

  function renderSourceOptions() {
    const input = $("source_channel_url");
    const selected = state.configDirty ? input.value : state.config?.source_channel_url || "";
    const options = '<option value="">不选择频道</option>' + savedChannels().map((row) =>
      `<option value="${escape(row.url)}">${escape(row.name)}</option>`).join("");
    if (input.dataset.options !== options) {
      input.innerHTML = options;
      input.dataset.options = options;
    }
    input.value = savedChannels().find((row) => row.key === channelName(selected))?.url || "";
    const parent = channelFor(input.value);
    const topic = $("source_topic_id");
    const topicId = state.configDirty ? Number(topic.value) : state.config?.source_topic_id || 0;
    html(topic, topicOptions(parent));
    topic.value = String(parent?.topics?.some((row) => row.topic_id === topicId) ? topicId : 0);
    $("source-topic-field").hidden = !parent?.forum;
  }

  function renderDataSync() {
    const sync = state.data?.data_sync;
    const labels = {disabled: "未启用", waiting: "等待同步", syncing: "正在同步", connected: "已连接", offline: "连接失败"};
    text("data-sync-status", sync ? [labels[sync.stage] || "状态未知",
      sync.enabled ? `待同步 ${sync.pending || 0} 条` : "",
      sync.conflicts ? `冲突 ${sync.conflicts} 条` : "",
      sync.last_sync ? `最近同步 ${updated(sync.last_sync)}` : "", sync.error || ""].filter(Boolean).join(" · ") : "未启用");
    $("data-sync-now").disabled = state.pending || !sync?.enabled || sync.stage === "syncing";
    $("data-sync-conflicts").hidden = !sync?.conflicts;
    const kinds = {channels: "频道", topics: "话题", messages: "消息", cursors: "读取游标", tasks: "任务"};
    html($("data-sync-conflict-list"), (sync?.conflict_rows || []).map((row) =>
      `<div class="sync-conflict"><strong>${escape(kinds[row.kind] || row.kind)} · ${escape(row.key)}</strong>
      ${row.local_deleted || row.server_deleted ? `<p>${row.local_deleted ? "本机已删除" : "服务器已删除"}</p>` : ""}
      ${(row.fields || []).map((field) => `<p><b>${escape(field.field)}</b><br>本机：${escape(field.local)}<br>服务器：${escape(field.server)}</p>`).join("")}</div>`).join(""));
    for (const id of ["data-sync-server", "data-sync-local"]) {
      $(id).hidden = !sync?.conflicts;
      $(id).disabled = state.pending || sync?.stage === "syncing";
    }
  }

  function render() {
    renderHeader();
    if (taskViews.has(state.view)) { renderMetrics(); renderList(); }
    if (state.view === "channels") renderChannels();
    renderNaming();
  }

  function namingIds() { return [...state.namingSelected].filter((id) => state.tasks.has(id)); }

  function openNaming() {
    if (isBusy()) return;
    const selected = [...state.selected].map((id) => state.tasks.get(id)).filter((task) => task?.download_status === "completed" && !task.remote_device);
    const tasks = state.selected.size ? selected : filteredTasks().filter((task) => task.download_status === "completed" && !task.remote_device);
    state.namingRows.clear(); state.namingDirty.clear(); state.namingSelected.clear();
    $("naming-list").replaceChildren();
    for (const task of tasks.slice(0, 100)) {
      const row = document.createElement("div");
      const id = String(task.id);
      row.className = "naming-row";
      row.innerHTML = `<input type="checkbox" checked aria-label="选择此名称"><div class="naming-picture"></div><div class="naming-original"><strong></strong><small></small></div><div class="naming-proposal"><label><span>中文文件名</span><input type="text" autocomplete="off" spellcheck="false" aria-label="中文文件名"></label><small role="status"></small></div>`;
      row.querySelector("input[type=checkbox]").addEventListener("change", (event) => {
        if (event.target.checked) state.namingSelected.add(id); else state.namingSelected.delete(id);
        renderNaming();
      });
      row.querySelector("input[type=text]").addEventListener("input", () => state.namingDirty.add(id));
      imageSlot(row.querySelector(".naming-picture"), task.images?.[0]);
      state.namingRows.set(id, row); state.namingSelected.add(id);
      $("naming-list").append(row);
    }
    text("naming-error", ""); $("naming-error").hidden = true;
    $("naming-dialog").showModal();
    renderNaming();
  }

  function renderNaming() {
    if (!$("naming-dialog").open) return;
    const labels = {idle: "尚未生成", generating: "正在翻译", review: "待确认", approved: "已确认，上传后应用",
      applying: "正在改名", completed: "已改名", failed: "需处理"};
    const reviewLabels = {partial: "含未识别名称，待确认", machine: "机器译名，待确认", matched: "术语已匹配，待确认"};
    let generated = 0;
    for (const [id, row] of state.namingRows) {
      const task = state.tasks.get(id);
      if (!task) continue;
      row.querySelector(".naming-original strong").textContent = task.filename;
      row.querySelector(".naming-original small").textContent = [
        task.remote_filename ? `云端：${task.remote_filename}` : "",
        task.naming_status === "review" ? reviewLabels[task.naming_quality] || "待确认" : labels[task.naming_status] || "尚未生成",
      ].filter(Boolean).join(" · ");
      const input = row.querySelector("input[type=text]");
      if (!state.namingDirty.has(id) && document.activeElement !== input) input.value = task.chinese_filename || "";
      input.disabled = isBusy();
      row.querySelector("input[type=checkbox]").disabled = isBusy();
      row.querySelector("input[type=checkbox]").checked = state.namingSelected.has(id);
      const note = row.querySelector(".naming-proposal small");
      const dirty = state.namingDirty.has(id);
      note.textContent = dirty ? "已编辑，待保存" : task.naming_error || task.naming_note || "";
      note.classList.toggle("failed", !dirty && Boolean(task.naming_error));
      note.classList.toggle("warning", !dirty && !task.naming_error && task.naming_quality === "partial");
      if (task.chinese_filename) generated++;
    }
    const selected = namingIds();
    $("naming-select-all").checked = selected.length > 0 && selected.length === state.namingRows.size;
    $("naming-select-all").indeterminate = selected.length > 0 && selected.length < state.namingRows.size;
    $("naming-select-all").disabled = isBusy();
    text("naming-summary", `已选 ${selected.length} / ${state.namingRows.size} · 已生成 ${generated}`);
    text("naming-result", state.data?.running && String(state.data.action).startsWith("names/")
      ? "正在处理，可关闭窗口后继续查看" : "已上传文件立即改名；待上传文件在上传后应用");
    for (const id of ["naming-generate", "naming-save", "naming-apply"]) $(id).disabled = isBusy() || !selected.length;
  }

  async function namingAction(kind) {
    const ids = namingIds();
    if (isBusy() || !ids.length) return;
    if (state.configDirty) { text("naming-error", "系统设置尚未保存，请先保存"); $("naming-error").hidden = false; return; }
    await mutation(async () => {
      $("naming-error").hidden = true;
      try {
        const dirty = ids.filter((id) => state.namingDirty.has(id));
        if (dirty.length) {
          await post("/api/names/save", {entries: dirty.map((id) => ({id, name: state.namingRows.get(id).querySelector("input[type=text]").value.trim()}))});
          for (const id of dirty) state.namingDirty.delete(id);
        }
        if (kind !== "save") await post(`/api/names/${kind}`, {ids});
        notice(kind === "generate" ? "已开始生成中文候选名" : kind === "apply" ? "已提交中文名称应用任务" : "中文草稿已保存");
      } catch (error) {
        text("naming-error", error.message); $("naming-error").hidden = false;
      }
    });
  }

  function setView(view, push = true) {
    if (!views[view]) view = "download";
    state.scroll[state.view] = window.scrollY;
    state.view = view;
    if (view !== "messages") {
      clearTimeout(prefetchTimer);
      prefetchTimer = null;
      prefetchKey = "";
    }
    closeChannelPicker(false);
    closeInspector();
    if (push) { const url = new URL(location.href); url.searchParams.set("view", view); history.pushState({}, "", url); }
    document.title = `${views[view]} · 素材工作台`;
    text("breadcrumb", views[view]); text("page-title", views[view]);
    for (const link of document.querySelectorAll(".nav-item")) {
      link.classList.toggle("active", link.dataset.view === view);
      if (link.dataset.view === view) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    $("metrics").hidden = !taskViews.has(view);
    $("channel-messages-section").hidden = view !== "messages";
    $("task-workspace").hidden = !taskViews.has(view);
    $("logs-section").hidden = view !== "history";
    $("settings-section").hidden = view !== "settings";
    $("channels-section").hidden = view !== "channels";
    $("task-search").value = state.filters[view].query;
    $("channel-filter").value = state.filters[view].chat;
    render();
    if (view === "messages") void refreshCatalog();
    requestAnimationFrame(() => window.scrollTo(0, state.scroll[view] || 0));
  }

  function goSettings(field = "source_channel_url") {
    setView("settings");
    requestAnimationFrame(() => { $(field)?.scrollIntoView({ block: "center" }); $(field)?.focus({ preventScroll: true }); });
  }

  function inspect(id, target) {
    state.active = id;
    state.returnFocus = target;
    for (const [rowId, row] of state.rows) row.classList.toggle("focused", rowId === id);
    renderInspector();
    if (matchMedia("(max-width: 900px)").matches) {
      $("inspector").classList.add("mobile-open");
      $("inspector").setAttribute("role", "dialog");
      $("inspector").setAttribute("aria-modal", "true");
      $("inspector").focus();
    }
  }

  function closeInspector() {
    if (!$("inspector").classList.contains("mobile-open")) return;
    $("inspector").classList.remove("mobile-open");
    $("inspector").removeAttribute("role");
    $("inspector").removeAttribute("aria-modal");
    if (state.returnFocus?.isConnected) state.returnFocus.focus({ preventScroll: true });
  }

  async function refreshWorkbench() {
    if (workbenchRequest) return workbenchRequest;
    workbenchRequest = (async () => {
      try {
        const data = await request("/api/workbench");
        if (!Array.isArray(data.tasks) || typeof data.running !== "boolean") throw new Error("/api/workbench：缺少有效 tasks 或 running 字段");
        const tasks = new Map();
        for (const task of data.tasks) {
          if (!task || !["string", "number"].includes(typeof task.id) || String(task.id) === "" || tasks.has(String(task.id))) throw new Error("/api/workbench：任务 ID 缺失或重复");
          tasks.set(String(task.id), { ...task, images: Array.isArray(task.images) ? task.images.filter((image) => image && typeof image.url === "string") : [] });
        }
        state.data = data;
        state.tasks = tasks;
        catalogTaskIndex = null;
        state.workbenchOK = true;
        state.lastSync = new Date();
        for (const id of state.selected) if (!tasks.has(id)) state.selected.delete(id);
        if (state.active && !tasks.has(state.active)) {
          state.active = null;
          closeInspector();
        }
        setError("workbench", ""); setError("backend", data.error ? `任务错误：${redact(data.error)}` : "");
        if (state.browserRefreshPending && !data.running) {
          state.browserRefreshPending = false;
          void refreshBrowserInfo();
        }
      } catch (error) {
        state.workbenchOK = false;
        setError("workbench", `${error.message}${state.data ? "；当前保留上次成功数据。" : ""}`, "workbench");
      } finally { render(); }
    })();
    try { await workbenchRequest; } finally { workbenchRequest = null; }
  }

  async function refreshCatalog() {
    if (!canReadChannel()) {
      if (state.catalog) clearChannelView();
      render();
      return;
    }
    if (catalogRequest) return catalogRequest;
    const revision = state.channelRevision;
    catalogRequest = (async () => {
      try {
        const data = await request("/api/channel-messages");
        if (!Array.isArray(data.messages) || typeof data.channel !== "string") throw new Error("/api/channel-messages：消息列表无效");
        if (revision !== state.channelRevision || !canReadChannel() || data.channel !== selectedScope()) return;
        if (state.catalog?.channel !== data.channel) {
          state.catalogSelection.clear();
          for (const row of state.catalogRows.values()) row.remove();
          state.catalogRows.clear();
        }
        state.catalog = data;
        setError("catalog", "");
      } catch (error) { setError("catalog", error.message, "catalog"); }
      render();
    })();
    try { await catalogRequest; } finally { catalogRequest = null; }
  }

  async function refreshChannels() {
    if (channelsRequest) return channelsRequest;
    channelsRequest = (async () => {
      try {
        const data = await request("/api/channels");
        if (!data || !Array.isArray(data.channels) || typeof data.active !== "string") throw new Error("/api/channels：频道列表无效");
        if (state.channels && (state.channels.active !== data.active
          || (state.channels.active_topic_id || 0) !== (data.active_topic_id || 0))) clearChannelView();
        state.channels = data;
        catalogTaskIndex = null;
        if (state.configLoaded && !state.configDirty && (channelName(state.config?.source_channel_url) !== data.active
          || (state.config?.source_topic_id || 0) !== (data.active_topic_id || 0))) await loadConfig();
        setError("channels", "");
      } catch (error) { setError("channels", error.message, "channels"); }
      render();
    })();
    try { await channelsRequest; } finally { channelsRequest = null; }
  }

  async function refreshJoinedChannels() {
    if (joinedChannelsRequest) return joinedChannelsRequest;
    joinedChannelsRequest = (async () => {
      try {
        const data = await request("/api/telegram-channels");
        if (!data || typeof data.stage !== "string") throw new Error("/api/telegram-channels：扫描状态无效");
        state.joinedChannels = data;
        setError("joined-channels", "");
      } catch (error) { setError("joined-channels", error.message, "joined-channels"); }
      render();
    })();
    try { await joinedChannelsRequest; } finally { joinedChannelsRequest = null; }
  }

  function renderLogs() {
    const query = $("log-search").value.trim().toLocaleLowerCase();
    const logs = state.logs?.filter((line) => !query || line.toLocaleLowerCase().includes(query));
    const value = logs ? logs.join("\n") || (query ? "没有匹配的日志" : "暂无运行日志") : state.errors.has("logs") ? "日志读取失败" : "正在读取日志";
    const box = $("logs");
    if (box.textContent !== value) {
      const top = box.scrollTop;
      box.textContent = value;
      box.scrollTop = $("follow-logs").checked ? box.scrollHeight : top;
    }
    text("latest-log", state.logs?.length ? state.logs[state.logs.length - 1] : state.logs ? "暂无运行日志" : state.errors.has("logs") ? "日志读取失败" : "正在读取日志");
  }

  async function refreshLogs() {
    if (logsRequest) return logsRequest;
    logsRequest = (async () => {
      try {
        const data = await request("/api/logs");
        if (!Array.isArray(data.logs)) throw new Error("/api/logs：缺少有效 logs 数组");
        state.logs = data.logs.map(redact);
        setError("logs", "");
      } catch (error) { setError("logs", error.message, "logs"); }
      renderLogs();
    })();
    try { await logsRequest; } finally { logsRequest = null; }
  }

  async function refreshAll() {
    await Promise.all([refreshWorkbench(), refreshLogs(), refreshChannels(), refreshJoinedChannels()]);
    if (state.view === "messages") await refreshCatalog();
  }

  async function poll() {
    clearTimeout(pollTimer);
    await refreshAll();
    pollTimer = setTimeout(poll, document.hidden ? 10000 : state.data?.running && state.data.channel_requests_open ? 1000 : 2000);
  }

  function markRequired() {
    const quark = $("upload_provider").value === "quark_browser";
    const quarkCLI = $("upload_provider").value === "quark_cli";
    const quarkAPI = $("upload_provider").value === "quark_api";
    const required = new Set(["recent_message_limit", "channel_message_limit", "download_start_timeout_seconds", "download_transfer_timeout_seconds", "download_workers", "download_file_concurrency", "inbox_dir"]);
    if (!state.apiIdConfigured) required.add("telegram_api_id");
    if (!state.apiConfigured) required.add("telegram_api_hash");
    if (quark) required.add("browser_executable_path");
    if (quarkAPI) {
      required.add("quark_api_parent_fid");
      if (!state.quarkConfigured) required.add("quark_cookie");
    }
    const database = $("data_server_backend").value !== "http";
    $("data-db-fields").hidden = !database;
    $("data-http-fields").hidden = database;
    $("data-db-check").hidden = !database;
    if (database && $("data_server_enabled").checked) {
      for (const id of ["data_server_db_host", "data_server_db_port", "data_server_db_name", "data_server_db_user"]) required.add(id);
      if (!state.dataPasswordConfigured) required.add("data_server_db_password");
    }
    for (const id of formFields) {
      $(id).required = required.has(id);
      $(id).setAttribute("aria-required", String(required.has(id)));
    }
    $("browser-path-field").hidden = !quark;
    $("browser-info").parentElement.hidden = !quark;
    $("browser-check").hidden = !quark;
    $("quark-fields").hidden = !quark;
    $("quark-cli-fields").hidden = !quarkCLI;
    $("quark-api-fields").hidden = !quarkAPI;
    $("quark-api-check").hidden = !quarkAPI;
    text("quark-cookie-status", state.quarkConfigured ? "已保存" : "未配置");
    $("quark-cli-login").hidden = !quarkCLI;
    $("quark-cli-check").hidden = !quarkCLI;
    $("quark-login").hidden = !quark;
    text("api-status", `API ID ${state.apiIdConfigured ? "已保存" : "未配置"} · API Hash ${state.apiConfigured ? "已保存" : "未配置"}`);
    renderHeader();
  }

  function showBrowser(config) {
    text("browser-info", redact(config.browser_profile_error || [config.browser_version ? `版本 ${config.browser_version}` : "版本未提供", config.browser_profile_status, config.browser_profile_dir].filter(Boolean).join(" | ")));
  }

  async function refreshBrowserInfo() {
    try { showBrowser(await request("/api/config")); setError("browser-info", ""); }
    catch (error) { setError("browser-info", `浏览器检查结果读取失败：${error.message}`, "browser-info"); }
  }

  async function loadConfig() {
    if (configRequest) return configRequest;
    const revision = state.formRevision;
    configRequest = (async () => {
      try {
        const config = await request("/api/config");
        if (config.telegram_access_mode !== "api" || !["local", "quark_browser", "quark_cli", "quark_api"].includes(config.upload_provider)) throw new Error("/api/config：缺少有效获取方式或上传方式");
        state.quarkConfigured = config.quark_cookie_configured === true;
        state.dataTokenConfigured = config.data_server_token_configured === true;
        state.dataPasswordConfigured = config.data_server_db_password_configured === true;
        // Hashes stay server-side; the public API ID can be displayed as saved.
        state.apiConfigured = config.telegram_api_configured === true;
        state.apiIdConfigured = config.telegram_api_id_configured === true || (Number.isInteger(config.telegram_api_id) && config.telegram_api_id > 0);
        state.config = {};
        for (const id of [...plainFields, ...numberFields, ...booleanFields, "download_dir", "unverified_dir", "log_path"]) if (config[id] !== undefined) state.config[id] = config[id];
        state.proxyProtected = /:\/\/[^\s/@]+@/.test(String(config.telegram_api_proxy_url || ""));
        if (state.proxyProtected) state.config.telegram_api_proxy_url = "";
        state.configLoaded = true;
        if (revision === state.formRevision) {
          for (const id of [...plainFields, ...numberFields]) {
            if (["source_channel_url", "source_topic_id"].includes(id)) continue;
            const input = $(id);
            const value = state.config[id];
            if (input instanceof HTMLSelectElement && value != null && ![...input.options].some((option) => option.value === String(value))) input.add(new Option(String(value), String(value)));
            if (value !== undefined) input.value = String(value ?? "");
          }
          for (const id of booleanFields) $(id).checked = state.config[id] === true;
          $("telegram_api_id").value = Number.isInteger(config.telegram_api_id) && config.telegram_api_id > 0 ? String(config.telegram_api_id) : "";
          $("telegram_api_hash").value = state.apiConfigured ? savedHashMask : "";
          $("quark_cookie").value = state.quarkConfigured ? savedHashMask : "";
          $("data_server_token").value = state.dataTokenConfigured ? savedHashMask : "";
          $("data_server_db_password").value = state.dataPasswordConfigured ? savedHashMask : "";
          $("telegram_api_hash").setAttribute("aria-label", state.apiConfigured ? "Telegram API Hash，已保存并隐藏" : "Telegram API Hash");
          state.configDirty = false;
          text("config-dirty", "");
        }
        text("config-download-dir", config.download_dir || "未提供");
        text("config-unverified-dir", config.unverified_dir || "未提供");
        text("config-log-path", config.log_path || "未提供");
        text("download-crypto-info", config.telegram_crypto_backend === "cryptg" ? "cryptg · 已启用" :
          config.telegram_crypto_backend === "OpenSSL" ? "OpenSSL · 已启用" : "Python · 未启用原生加速");
        showBrowser(config);
        $("reload-config").hidden = true;
        setError("config", ""); markRequired(); render();
        return true;
      } catch (error) {
        setError("config", error.message, state.configLoaded ? "" : "config");
        $("reload-config").hidden = state.configLoaded;
        renderHeader();
        return false;
      }
    })();
    try { return await configRequest; } finally { configRequest = null; }
  }

  function failField(id, message) {
    $(id).classList.add("field-error"); $(id).setAttribute("aria-invalid", "true");
    text("form-error", message); $("form-error").hidden = false;
    if (state.view !== "settings") goSettings(id); else { $(id).focus(); $(id).scrollIntoView({ block: "center" }); }
    return false;
  }

  function validate(purpose = "save") {
    $("form-error").hidden = true;
    for (const id of formFields) { $(id).classList.remove("field-error"); $(id).removeAttribute("aria-invalid"); }
    if ($("data_server_enabled").checked && $("data_server_backend").value === "http") {
      let endpoint;
      try { endpoint = new URL($("data_server_url").value.trim()); } catch { return failField("data_server_url", "请填写数据服务地址"); }
      if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password
          || endpoint.pathname !== "/" || endpoint.search || endpoint.hash
          || (endpoint.protocol === "http:" && !["127.0.0.1", "localhost", "[::1]"].includes(endpoint.hostname))) {
        return failField("data_server_url", "远程使用 HTTPS，SSH 隧道使用 http://127.0.0.1:8780");
      }
      const token = $("data_server_token").value.trim();
      if ((!token && !state.dataTokenConfigured) || (token && !(state.dataTokenConfigured && token === savedHashMask) && !/^[!-~]{32,}$/.test(token))) {
        return failField("data_server_token", "请填写至少 32 位的访问令牌");
      }
    }
    if ($("data_server_backend").value !== "http") {
      if (!/^[A-Za-z0-9_.:\-]{1,253}$/.test($("data_server_db_host").value.trim())) return failField("data_server_db_host", "请填写数据库 IP 或主机名");
      for (const id of ["data_server_db_name", "data_server_db_user"]) {
        if (!/^[A-Za-z0-9_.\-]{1,64}$/.test($(id).value.trim())) return failField(id, "请填写有效的数据库名和用户");
      }
      if ($("data_server_enabled").checked) {
        if (!$("data_server_db_password").value && !state.dataPasswordConfigured) return failField("data_server_db_password", "请填写数据库密码");
        if ($("data_server_db_tls").value === "disable" && !["127.0.0.1", "localhost", "::1"].includes($("data_server_db_host").value.trim())) {
          return failField("data_server_db_tls", "远程连接须使用 TLS；SSH 隧道请填写本机地址 127.0.0.1");
        }
      }
    }
    if (!/^[A-Za-z0-9_-]{1,64}$/.test($("data_server_workspace").value.trim())) return failField("data_server_workspace", "工作空间只能包含字母、数字、下划线或连字符");
    const directory = $("inbox_dir").value.trim();
    if (!directory) return failField("inbox_dir", "请填写下载保存目录");
    if (!/^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+\\)/.test(directory)) return failField("inbox_dir", "请填写完整的绝对目录路径");
    const quark = $("upload_provider").value === "quark_browser";
    const quarkCLI = $("upload_provider").value === "quark_cli";
    const quarkAPI = $("upload_provider").value === "quark_api";
    const cookie = $("quark_cookie").value.trim();
    if (quarkAPI && !cookie && !state.quarkConfigured) return failField("quark_cookie", "请填写夸克 Cookie");
    if (cookie && cookie !== savedHashMask && (!cookie.includes("=") || /[^\x20-\x7e]/.test(cookie))) return failField("quark_cookie", "请粘贴有效的 Cookie 请求头值");
    if (quarkAPI && !/^[A-Za-z0-9_-]{1,128}$/.test($("quark_api_parent_fid").value.trim())) return failField("quark_api_parent_fid", "请填写有效目录 FID，根目录填 0");
    for (const id of numberFields) {
      const value = Number($(id).value);
      if (!$(id).value || !Number.isInteger(value) || value < Number($(id).min) || value > Number($(id).max)) return failField(id, `请填写 ${$(id).min} 到 ${$(id).max} 的整数：${$(id).closest("label").firstChild.textContent.trim()}`);
    }
    if ((quark || purpose === "browser" || purpose === "quark") && !$("browser_executable_path").value.trim()) return failField("browser_executable_path", "请填写浏览器程序路径");
    if ((purpose === "quark-cli" || quarkCLI) && !$("quark_cli_executable_path").value.trim()) return failField("quark_cli_executable_path", "请填写夸克 CLI 执行程序或命令");
    if ((purpose === "quark-cli" || quarkCLI) && $("quark_cli_mode").value === "wsl"
        && !$("quark_cli_script_path").value.trim()
        && $("quark_cli_node_command").value.trim().toLowerCase() !== "quarkclouddrive") {
      return failField("quark_cli_script_path", "WSL 模式请填写 quark-drive.cjs 路径");
    }
    const parentFid = $("quark_cli_parent_fid").value.trim();
    if (parentFid && !/^[A-Za-z0-9_-]+$/.test(parentFid)) return failField("quark_cli_parent_fid", "目标目录 FID 只能包含字母、数字、下划线或连字符");
    const id = $("telegram_api_id").value.trim();
    if ((id || !state.apiIdConfigured) && (!/^\d+$/.test(id) || Number(id) < 1 || Number(id) > 2147483647)) return failField("telegram_api_id", "Telegram API ID 必须为有效正整数");
    const hash = $("telegram_api_hash").value.trim();
    if (!(state.apiConfigured && hash === savedHashMask)
        && (hash || !state.apiConfigured) && !/^[a-f0-9]{32}$/i.test(hash)) return failField("telegram_api_hash", "Telegram API Hash 必须为 32 位十六进制字符");
    const source = $("source_channel_url").value.trim();
    if ((source || purpose === "download") && !/^(?:https:\/\/t\.me\/[A-Za-z0-9_]+\/?|-100[1-9][0-9]*)$/.test(source)) return failField("source_channel_url", "请填写有效的 Telegram 频道链接或 ID");
    const folder = $("quark_folder_url").value.trim();
    if ((folder || purpose === "quark" || (purpose === "upload" && quark)) && !externalURL(folder)) return failField("quark_folder_url", "请填写有效的夸克目标文件夹 URL");
    return true;
  }

  function values() {
    const output = { telegram_access_mode: "api", telegram_run_mode: "once" };
    for (const id of plainFields) output[id] = $(id).value.trim();
    for (const id of numberFields) if ($(id).value) output[id] = Number($(id).value);
    for (const id of booleanFields) output[id] = $(id).checked;
    if (state.proxyProtected && !output.telegram_api_proxy_url) delete output.telegram_api_proxy_url;
    const id = $("telegram_api_id").value.trim();
    const hash = $("telegram_api_hash").value.trim();
    if (id) output.telegram_api_id = Number(id);
    if (hash && !(state.apiConfigured && hash === savedHashMask)) output.telegram_api_hash = hash;
    const cookie = $("quark_cookie").value.trim();
    if (cookie && !(state.quarkConfigured && cookie === savedHashMask)) output.quark_cookie = cookie;
    const token = $("data_server_token").value.trim();
    if (token && !(state.dataTokenConfigured && token === savedHashMask)) output.data_server_token = token;
    const password = $("data_server_db_password").value;
    if (password && !(state.dataPasswordConfigured && password === savedHashMask)) output.data_server_db_password = password;
    // Omit non-form fields so advanced configuration remains unchanged.
    return output;
  }

  async function saveConfig() {
    const payload = values();
    await post("/api/config", payload);
    if (payload.telegram_api_hash) state.apiConfigured = true;
    if (payload.telegram_api_id) state.apiIdConfigured = true;
    if (payload.telegram_api_hash) $("telegram_api_hash").value = savedHashMask;
    if (!await loadConfig()) {
      notice("配置已保存，但重新读取失败；请检查接口提示。");
      return false;
    }
    return true;
  }

  async function mutation(operation) {
    if (state.pending) return;
    state.pending = true;
    setError("action", "");
    render();
    try { await operation(); }
    catch (error) { setError("action", error.message); }
    finally { state.pending = false; await refreshAll(); render(); }
  }

  async function configuredAction(path, purpose, success) {
    if (isBusy() || !state.configLoaded) return;
    if (!validate(purpose)) return;
    await mutation(async () => {
      if (!await saveConfig()) return;
      await post(path);
      if (purpose === "browser") state.browserRefreshPending = true;
      notice(success);
    });
  }

  async function uploadTasks(tasks) {
    if (isBusy() || !tasks.length) return;
    if (state.configDirty) {
      setError("action", "设置尚未保存，请先保存配置再上传或获取链接。");
      goSettings("upload_provider"); return;
    }
    const ids = tasks.map((task) => task.id);
    if (ids.length > 100) { setError("action", "每次最多处理 100 个素材，请减少选中数量。"); return; }
    await mutation(async () => {
      await post("/api/upload", { ids });
      notice(tasks.every((task) => task.upload_status === "link_pending") ? "已请求获取现有文件的分享链接" : `已提交 ${ids.length} 个素材`);
    });
  }

  async function retryTask(task) {
    if (!task || isBusy() || !canRetry(task)) return;
    await mutation(async () => { await post("/api/retry", { id: task.id }); notice("已请求重新下载"); });
  }

  async function deleteCloudTask(task) {
    if (isBusy() || !canDeleteCloud(task)) return;
    if (state.configDirty || state.config?.upload_provider !== "quark_api") {
      setError("action", "请先在设置中保存夸克 API 配置，再删除云端文件。");
      goSettings("upload_provider"); return;
    }
    const label = deleteCloudLabel(task);
    if (!window.confirm(`${label}？\n\n${task.remote_filename || task.upload_filename || task.filename}\n原上传目录 FID：${task.upload_target}\n\n云端分享链接可能失效，本地文件和任务记录将保留。`)) return;
    await mutation(async () => {
      await post("/api/cloud-delete", { id: task.id, remote_fid: task.upload_remote_fid, confirm: true });
      notice("已提交云端删除核对，请等待任务结果");
    });
  }

  async function removeRecord(task) {
    if (isBusy() || !canRemoveRecord(task) || removalBlocked(task)) return;
    if (!window.confirm(`移除此条素材记录？\n\n${taskName(task)}\n\n该记录将从素材库和各个任务列表移除，本地文件、图片和夸克文件保留。`)) return;
    await mutation(async () => {
      await post("/api/library/remove", { id: task.id, confirm: true });
      notice("记录已移除，本地文件和云端文件保留");
    });
  }

  async function scanChannel(older = false) {
    if (isBusy() || !canReadChannel()) return;
    if (state.configDirty) { setError("action", "请先保存设置，再读取频道消息。"); return; }
    state.scanRequested = true;
    try {
      await mutation(async () => {
        await post("/api/channel-scan", { older });
      });
    } finally { state.scanRequested = false; render(); }
  }

  async function scanJoinedChannels() {
    if (isBusy() || !state.configLoaded) return;
    if (state.configDirty) { setError("action", "请先保存设置，再获取账号频道。"); return; }
    if (!state.apiConfigured || !state.apiIdConfigured) { goSettings("telegram_api_id"); return; }
    state.joinedScanRequested = true;
    closeChannelPicker(false);
    try {
      await mutation(async () => {
        await post("/api/telegram-channels-scan", {});
      });
    } finally { state.joinedScanRequested = false; render(); }
  }

  function clearChannelView() {
    state.channelRevision++;
    state.catalogSelection.clear();
    state.previewErrors.clear();
    state.previewRequested.clear();
    state.catalog = null;
    for (const row of state.catalogRows.values()) row.remove();
    state.catalogRows.clear();
    $("channel-search").value = "";
    $("channel-transfer-filter").value = "all";
  }

  async function scanTopics(key) {
    if (isBusy() || !key) return;
    if (state.configDirty) { setError("action", "请先保存设置，再获取话题。"); return; }
    state.topicScanRequested = key;
    try {
      await mutation(async () => { await post("/api/channel-topics-scan", { key }); });
    } finally { state.topicScanRequested = ""; render(); }
  }

  async function selectChannel(key, topicId = 0) {
    if (isBusy() || !key || !savedChannels().some((row) => row.key === key)) return;
    if (state.configDirty) { setError("action", "请先保存设置，再切换频道。"); return; }
    closeChannelPicker();
    await mutation(async () => {
      const result = await post("/api/channels/select", topicId ? { key, topic_id: topicId } : { key });
      clearChannelView();
      await loadConfig();
      const topic = result.channel?.topics?.find((row) => row.topic_id === topicId)
        || savedChannels().find((row) => row.key === key)?.topics?.find((row) => row.topic_id === topicId);
      notice(`已切换到 ${result.channel?.name || `@${key}`}${topic ? ` / ${topic.title}` : ""}`);
    });
  }

  function openChannelEditor(mode = "add", row = null) {
    if (channelDirectoryBusy()) return;
    state.channelEditorMode = mode;
    state.channelEditorKey = row?.key || "";
    text("channel-editor-title", mode === "edit" ? "编辑频道" : "添加频道");
    $("channel-editor-name").value = row?.name || "";
    $("channel-editor-url").value = row?.url || "";
    $("channel-editor-url").disabled = mode === "edit";
    text("channel-editor-hint", mode === "edit" ? "频道链接不可修改；如需更换，请添加新的频道。" : "仅支持公开频道链接。");
    $("channel-editor-error").hidden = true;
    $("channel-editor-dialog").showModal();
    $("channel-editor-name").focus();
  }

  function closeChannelEditor() {
    if ($("channel-editor-dialog").open) $("channel-editor-dialog").close();
  }

  async function saveChannelEditor() {
    const name = $("channel-editor-name").value.trim();
    const url = $("channel-editor-url").value.trim();
    const valid = /^https?:\/\/t\.me\/[A-Za-z0-9_+\-]+\/?$/i.test(url);
    if (state.channelEditorMode === "add" && !valid) {
      text("channel-editor-error", "请填写有效的 Telegram 公开频道链接。");
      $("channel-editor-error").hidden = false;
      $("channel-editor-url").focus();
      return;
    }
    if (state.channelEditorMode === "edit" && !name) {
      text("channel-editor-error", "频道名称不能为空。");
      $("channel-editor-error").hidden = false;
      $("channel-editor-name").focus();
      return;
    }
    await mutation(async () => {
      if (state.channelEditorMode === "add") {
        const result = await post("/api/channels", { url, name });
        notice(`已保存 ${name || `@${result.channel.key}`}`);
      } else {
        await post("/api/channels/rename", { key: state.channelEditorKey, name });
        notice("频道名称已更新");
      }
      closeChannelEditor();
    });
  }

  async function removeChannel(key) {
    if (channelDirectoryBusy()) return;
    const row = (state.channels?.channels || []).find((item) => item.key === key);
    if (!row || !window.confirm(`确定移除“${row.name}”吗？已下载任务和消息缓存不会删除。`)) return;
    await mutation(async () => {
      await post("/api/channels/remove", { key });
      notice("频道已从列表移除");
    });
  }

  async function downloadChannelMessages(ids) {
    if (!canLoadChannelMedia() || !ids.length) return;
    if (ids.length > 20) { setError("action", "每次最多下载 20 条消息，请减少选择。"); return; }
    if (state.configDirty) { setError("action", "请先保存设置，再下载频道文件。"); return; }
    await mutation(async () => {
      await post("/api/channel-download", { ids });
      notice(`已提交 ${ids.length} 条下载任务`);
    });
  }

  async function stopPageTask() {
    if (!taskBelongsToPage() || $("stop").disabled) return;
    const task = { task_id: $("stop").dataset.taskId, action: $("stop").dataset.action };
    await mutation(async () => {
      await post("/api/stop", task);
      notice("已请求停止指定任务，等待任务响应");
    });
  }

  async function copy(value, label) {
    try { if (!navigator.clipboard) throw new Error("当前环境不支持剪贴板"); await navigator.clipboard.writeText(value); notice(`${label}已复制`); }
    catch (error) { setError("action", `复制失败：${error.message}`); }
  }

  async function exportDownloadDiagnostics() {
    const button = $("export-download-diagnostics");
    button.disabled = true;
    try {
      const response = await fetch("/api/download-diagnostics/export", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok || !response.headers.get("Content-Type")?.startsWith("application/zip")) {
        throw new Error("诊断文件读取失败，请刷新页面后重试");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "telegram-download-diagnostics.zip";
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      notice("下载诊断已生成");
    } catch (error) {
      setError("action", `导出下载诊断失败：${redact(error.message)}`);
    } finally {
      button.disabled = false;
    }
  }

  document.addEventListener("click", (event) => {
    if (state.pickerOpen && !event.target.closest("#channel-picker")) closeChannelPicker(false);
    const nav = event.target.closest("a[data-view], button[data-view]");
    if (nav && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); setView(nav.dataset.view); return; }
    const settings = event.target.closest("[data-go-settings]");
    if (settings) { goSettings(settings.dataset.goSettings); return; }
    const management = event.target.closest("[data-channel-management]");
    if (management) {
      const key = management.dataset.channelKey;
      const row = (state.channels?.channels || []).find((item) => item.key === key);
      if (management.dataset.channelManagement === "select") void selectChannel(key, Number(management.dataset.topicId) || 0);
      if (management.dataset.channelManagement === "topics") {
        if (state.expandedTopics.has(key)) state.expandedTopics.delete(key);
        else {
          state.expandedTopics.add(key);
          if (!row?.topics_scanned_at && !(row?.topics || []).length) void scanTopics(key);
        }
        renderChannels();
      }
      if (management.dataset.channelManagement === "refresh-topics") void scanTopics(key);
      if (management.dataset.channelManagement === "edit") openChannelEditor("edit", row);
      if (management.dataset.channelManagement === "remove") void removeChannel(key);
      return;
    }
    const channel = event.target.closest("[data-channel]");
    if (channel) { state.filters.download.chat = channel.dataset.channel; state.filters.download.status = "all"; state.filters.download.query = ""; setView("download"); return; }
    const tab = event.target.closest("[data-filter]");
    if (tab) { state.filters[state.view].status = tab.dataset.filter; renderList(); return; }
    const retry = event.target.closest("[data-retry]");
    if (retry) {
      const actions = { workbench: refreshWorkbench, logs: refreshLogs, catalog: refreshCatalog, channels: refreshChannels, config: loadConfig, "browser-info": refreshBrowserInfo, images: () => {
        state.brokenImages.clear(); setError("images", "");
        $("preview-strip").dataset.gallery = "";
        renderList();
      }, "joined-channels": refreshJoinedChannels };
      void actions[retry.dataset.retry]?.(); return;
    }
    const dismiss = event.target.closest("[data-dismiss-error]");
    if (dismiss) setError(dismiss.dataset.dismissError, "");
  });

  $("task-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-task-action]");
    if (!button) return;
    const id = button.closest("[data-task-id]").dataset.taskId;
    const task = state.tasks.get(id);
    if (button.dataset.taskAction === "inspect") inspect(id, button);
    if (button.dataset.taskAction === "retry") void retryTask(task);
    if (button.dataset.taskAction === "link" && task?.upload_status === "link_pending") void uploadTasks([task]);
    if (button.dataset.taskAction === "delete-cloud") void deleteCloudTask(task);
    if (button.dataset.taskAction === "remove-record") void removeRecord(task);
  });
  $("task-list").addEventListener("change", (event) => {
    if (!event.target.matches(".task-checkbox")) return;
    const id = event.target.closest("[data-task-id]").dataset.taskId;
    if (event.target.checked) state.selected.add(id); else state.selected.delete(id);
    event.target.closest(".task").classList.toggle("checked", event.target.checked);
    renderSelection();
  });
  $("channel-message-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-channel-action]");
    if (!button) return;
    const message = state.catalog?.messages.find((item) => item.message_id === Number(button.closest("[data-message-id]")?.dataset.messageId));
    if (!message) return;
    if (button.dataset.channelAction === "preview") openChannelPreview(message, button);
    if (button.dataset.channelAction === "download" && locallyDownloadableMessage(message)) {
      const ids = message.series
        ? (state.catalog?.messages || []).filter((item) => item.series_key === message.series_key && locallyDownloadableMessage(item)).map((item) => item.message_id)
        : [message.message_id];
      void downloadChannelMessages(ids);
    }
    if (button.dataset.channelAction === "task") {
      const task = catalogTransfer(message).task;
      if (task) { state.active = String(task.id); setView("download"); }
    }
  });
  $("channel-preview-close").addEventListener("click", () => $("channel-preview-dialog").close());
  $("channel-preview-dialog").addEventListener("click", (event) => {
    if (event.target === $("channel-preview-dialog")) $("channel-preview-dialog").close();
  });
  $("channel-preview-dialog").addEventListener("close", () => {
    $("channel-preview-image").removeAttribute("src");
    if (state.previewReturnFocus?.isConnected) state.previewReturnFocus.focus({ preventScroll: true });
    state.previewReturnFocus = null;
    state.previewMessageId = null;
  });
  $("channel-preview-original").addEventListener("click", () => {
    if (state.previewMessageId) void loadChannelFullImage(state.previewMessageId, true);
  });
  $("channel-preview-retry").addEventListener("click", () => {
    if (state.previewMessageId) void loadChannelFullImage(state.previewMessageId);
  });
  $("channel-preview-image").addEventListener("error", () => {
    if ($("channel-preview-dialog").open) $("channel-preview-dialog").close();
    setError("images", "频道封面加载失败，请重新读取频道后再试。");
  });
  $("channel-zoom-in").addEventListener("click", () => {
    state.previewZoom = Math.min(3, state.previewZoom + .5); updatePreviewZoom();
  });
  $("channel-zoom-out").addEventListener("click", () => {
    state.previewZoom = Math.max(1, state.previewZoom - .5); updatePreviewZoom();
  });
  $("channel-message-list").addEventListener("change", (event) => {
    if (!event.target.matches(".channel-checkbox")) return;
    const id = Number(event.target.closest("[data-message-id]").dataset.messageId);
    if (event.target.checked) state.catalogSelection.add(id); else state.catalogSelection.delete(id);
    renderCatalog();
  });
  $("channel-select-all").addEventListener("change", () => {
    for (const message of visibleChannelMessages().filter(downloadableMessage)) {
      if ($("channel-select-all").checked) state.catalogSelection.add(message.message_id);
      else state.catalogSelection.delete(message.message_id);
    }
    renderCatalog();
  });
  $("channel-search").addEventListener("input", renderCatalog);
  $("channel-transfer-filter").addEventListener("change", renderCatalog);
  $("channel-download-selected").addEventListener("click", () => {
    const selected = (state.catalog?.messages || []).filter((message) => state.catalogSelection.has(message.message_id) && downloadableMessage(message));
    const selectedSeries = new Set(selected.filter((message) => message.series).map((message) => message.series_key));
    const members = (state.catalog?.messages || []).filter((message) => message.series && selectedSeries.has(message.series_key) && downloadableMessage(message));
    const ids = [...new Map([...selected, ...members].map((message) => [message.message_id, message])).values()]
      .map((message) => message.message_id);
    void downloadChannelMessages(ids);
  });
  $("channel-load-older").addEventListener("click", () => void scanChannel(true));
  $("channel-empty-action").addEventListener("click", () => void scanChannel());
  $("select-all").addEventListener("change", () => {
    for (const task of filteredTasks().filter((task) => !task.remote_device)) { if ($("select-all").checked) state.selected.add(String(task.id)); else state.selected.delete(String(task.id)); }
    renderList();
  });
  $("clear-selection").addEventListener("click", () => { state.selected.clear(); renderList(); });
  $("task-search").addEventListener("input", () => { state.filters[state.view].query = $("task-search").value; renderList(); });
  $("channel-filter").addEventListener("change", () => { state.filters[state.view].chat = $("channel-filter").value; renderList(); });
  $("clear-filters").addEventListener("click", () => {
    state.filters[state.view] = { query: "", status: "all", chat: "" }; $("task-search").value = ""; $("channel-filter").value = ""; renderList();
  });
  $("preview-strip").addEventListener("click", (event) => {
    const button = event.target.closest("[data-image-index]");
    if (button) { state.images.set(state.active, Number(button.dataset.imageIndex)); renderInspector(); }
  });
  $("close-inspector").addEventListener("click", closeInspector);
  document.addEventListener("keydown", (event) => {
    if (!$("inspector").classList.contains("mobile-open")) return;
    if (event.key === "Escape") { event.preventDefault(); closeInspector(); }
    if (event.key === "Tab") {
      const controls = [...$("inspector").querySelectorAll("a[href], button:not(:disabled), [tabindex='0']")].filter((element) => element.getClientRects().length);
      const first = controls[0]; const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === $("inspector"))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === $("inspector"))) { event.preventDefault(); first?.focus(); }
    }
  });
  matchMedia("(max-width: 900px)").addEventListener("change", closeInspector);
  $("refresh").addEventListener("click", () => void refreshAll());
  $("refresh-logs").addEventListener("click", () => void refreshLogs());
  $("reload-config").addEventListener("click", () => { if (!state.configLoaded) void loadConfig(); });
  $("download").addEventListener("click", () => void configuredAction("/api/download", "download", "已请求扫描并下载"));
  $("scan-channel").addEventListener("click", () => void scanChannel());
  $("channel-select").addEventListener("click", () => state.pickerOpen ? closeChannelPicker() : openChannelPicker());
  $("channel-topic-select").addEventListener("change", (event) => {
    if (selectedChannel()) void selectChannel(selectedChannel().key, Number(event.target.value));
  });
  $("refresh-channel-topics").addEventListener("click", () => {
    if (selectedChannel()) void scanTopics(selectedChannel().key);
  });
  $("source_channel_url").addEventListener("change", () => {
    $("source_topic_id").value = "0";
    renderSourceOptions();
  });
  $("channel-select").addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); openChannelPicker(); }
  });
  $("channel-picker-search").addEventListener("input", () => {
    state.pickerFocusKey = "";
    renderChannelOptions();
  });
  $("channel-picker-search").addEventListener("keydown", (event) => {
    const rows = filteredChannelOptions();
    const index = rows.findIndex((row) => row.key === state.pickerFocusKey);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? rows.length - 1
        : event.key === "ArrowDown" ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1);
      state.pickerFocusKey = rows[next]?.key || "";
      highlightChannelOption(true);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (state.pickerFocusKey) void selectChannel(state.pickerFocusKey);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeChannelPicker();
    }
  });
  $("channel-options").addEventListener("click", (event) => {
    const option = event.target.closest("[data-channel-option]");
    if (option) void selectChannel(option.dataset.channelKey);
  });
  $("channel-picker").addEventListener("focusout", () => {
    requestAnimationFrame(() => {
      if (state.pickerOpen && !$("channel-picker").contains(document.activeElement)) closeChannelPicker(false);
    });
  });
  document.addEventListener("error", (event) => {
    if (event.target instanceof HTMLImageElement && event.target.closest(".channel-avatar")) {
      state.failedAvatars.add(event.target.getAttribute("src"));
      renderChannelSwitcher();
      if (state.view === "channels") renderChannels();
    }
  }, true);
  $("add-channel").addEventListener("click", () => openChannelEditor());
  $("channel-add-section").addEventListener("click", () => openChannelEditor());
  $("scan-joined-channels").addEventListener("click", () => void scanJoinedChannels());
  $("channel-editor-close").addEventListener("click", closeChannelEditor);
  $("channel-editor-cancel").addEventListener("click", closeChannelEditor);
  $("channel-editor-form").addEventListener("submit", (event) => { event.preventDefault(); if (!channelDirectoryBusy()) void saveChannelEditor(); });
  $("upload").addEventListener("click", () => void uploadTasks(selectedUploadTasks()));
  $("open-naming").addEventListener("click", openNaming);
  $("naming-close").addEventListener("click", () => $("naming-dialog").close());
  $("naming-select-all").addEventListener("change", (event) => {
    state.namingSelected = event.target.checked ? new Set(state.namingRows.keys()) : new Set();
    renderNaming();
  });
  $("naming-generate").addEventListener("click", () => void namingAction("generate"));
  $("naming-save").addEventListener("click", () => void namingAction("save"));
  $("naming-apply").addEventListener("click", () => void namingAction("apply"));
  $("retry-link").addEventListener("click", () => { if (activeTask()?.upload_status === "link_pending") void uploadTasks([activeTask()]); });
  $("retry-task").addEventListener("click", () => void retryTask(activeTask()));
  $("delete-cloud-task").addEventListener("click", () => void deleteCloudTask(activeTask()));
  $("remove-record-task").addEventListener("click", () => void removeRecord(activeTask()));
  $("stop").addEventListener("click", () => void stopPageTask());
  $("open-downloads").addEventListener("click", () => void mutation(async () => { await post("/api/open-downloads"); notice("已请求打开下载目录"); }));
  $("open-task").addEventListener("click", () => {
    const task = activeTask();
    if (task) void mutation(async () => { await post("/api/open-task", { id: task.id }); notice("已请求打开本地目录"); });
  });
  $("copy-share").addEventListener("click", () => { const url = externalURL(activeTask()?.share_url); if (url) void copy(url, "分享链接"); });
  $("copy-logs").addEventListener("click", () => { if (state.logs) void copy(state.logs.join("\n"), "日志"); });
  $("export-download-diagnostics").addEventListener("click", () => void exportDownloadDiagnostics());
  $("activity").addEventListener("click", () => { setView("history"); requestAnimationFrame(() => $("logs-section").scrollIntoView({ block: "start" })); });
  $("edit-channel").addEventListener("click", () => goSettings());
  $("log-search").addEventListener("input", renderLogs);
  $("follow-logs").addEventListener("change", () => { if ($("follow-logs").checked) $("logs").scrollTop = $("logs").scrollHeight; });
  $("logs").addEventListener("scroll", () => {
    if ($("logs").scrollTop + $("logs").clientHeight < $("logs").scrollHeight - 20) $("follow-logs").checked = false;
  });
  $("config-form").addEventListener("input", () => {
    state.configDirty = true; state.formRevision++; text("config-dirty", "未保存更改");
  });
  $("upload_provider").addEventListener("change", markRequired);
  $("data_server_enabled").addEventListener("change", markRequired);
  $("data_server_backend").addEventListener("change", () => {
    const backend = $("data_server_backend").value;
    const port = $("data_server_db_port");
    if (backend === "mysql" && port.value === "5432") port.value = "3306";
    else if (backend === "postgresql" && port.value === "3306") port.value = "5432";
    markRequired();
  });
  $("data-db-check").addEventListener("click", () => {
    if (state.configDirty) { setError("action", "请先保存数据库设置"); return; }
    void mutation(async () => {
      $("data-db-check").disabled = true;
      $("data-db-check-result").hidden = false;
      text("data-db-check-result", "正在测试数据库连接…");
      try {
        const result = await post("/api/data-check");
        text("data-db-check-result", result.message); notice(result.message);
      } catch (error) {
        text("data-db-check-result", error.message);
        throw error;
      } finally { $("data-db-check").disabled = false; }
    });
  });
  for (const [id, choice] of [["data-sync-now", null], ["data-sync-server", "server"], ["data-sync-local", "local"]]) {
    $(id).addEventListener("click", () => {
      if (state.configDirty) { setError("action", "请先保存服务器设置"); return; }
      if (choice && !confirm(choice === "server" ? "用服务器版本替换所有冲突记录？" : "用本机版本覆盖所有冲突记录？")) return;
      void mutation(async () => { await post("/api/data-sync", choice ? {resolve: choice} : {}); notice("已请求同步"); });
    });
  }
  $("telegram_api_hash").addEventListener("focus", () => {
    if (state.apiConfigured && $("telegram_api_hash").value === savedHashMask) $("telegram_api_hash").select();
  });
  $("config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (isBusy() || !state.configLoaded || !validate()) return;
    void mutation(async () => { if (await saveConfig()) notice("配置已保存"); });
  });
  $("browser-check").addEventListener("click", () => void configuredAction("/api/browser-check", "browser", "已请求检查浏览器"));
  $("telegram-login").addEventListener("click", () => void configuredAction("/api/telegram-api-login", "telegram", "已请求打开 Telegram 登录"));
  $("quark-login").addEventListener("click", () => void configuredAction("/api/quark-login", "quark", "已请求打开夸克网页登录"));
  $("quark-cli-login").addEventListener("click", () => void configuredAction("/api/quark-cli-login", "quark-cli", "已请求打开夸克 CLI 登录"));
  $("quark-cli-check").addEventListener("click", () => void configuredAction("/api/quark-cli-check", "quark-cli", "已请求检查夸克 CLI 授权"));
  $("quark-api-check").addEventListener("click", () => void configuredAction("/api/quark-api-check", "quark-api", "已请求检查夸克 API 登录及目标目录"));
  window.addEventListener("popstate", () => setView(new URLSearchParams(location.search).get("view"), false));
  window.addEventListener("beforeunload", (event) => { if (state.configDirty) { event.preventDefault(); event.returnValue = ""; } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void refreshAll(); });

  icons();
  if (!window.lucide) setError("icons", "本地图标库加载失败：/static/lucide.js");
  setView(new URLSearchParams(location.search).get("view"), false);
  void loadConfig();
  void poll();
})();
