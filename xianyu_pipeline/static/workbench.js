/* Same-origin API client. Polling never loads form values or replaces task rows. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const views = { download: "下载任务", upload: "上传任务", library: "素材库", history: "任务记录", channels: "来源频道", settings: "设置" };
  const downloadStates = {
    queued: ["等待下载", "gray"], downloading: ["下载中", ""], validating: ["校验中", "amber"],
    completed: ["已下载", ""], failed: ["下载失败", "red"], cancelled: ["已取消", "gray"], interrupted: ["下载中断", "red"]
  };
  const uploadStates = {
    waiting: ["待上传", "gray"], uploading: ["上传中", ""], link_pending: ["结果待确认 / 链接待获取", "amber"],
    completed: ["已完成", ""], local: ["本地模拟", "gray"], failed: ["上传失败", "red"]
  };
  const retryable = new Set(["failed", "cancelled", "interrupted"]);
  const taskViews = new Set(["download", "upload", "library", "history"]);
  const numberFields = ["recent_message_limit", "telegram_browser_timeout_seconds", "download_start_timeout_seconds", "download_transfer_timeout_seconds"];
  const booleanFields = ["telegram_browser_headless", "quark_headless"];
  const plainFields = ["source_channel_url", "telegram_access_mode", "telegram_api_proxy_url", "telegram_api_dc2_server", "inbox_dir", "browser_executable_path", "archive_test_executable_path", "telegram_run_mode", "upload_provider", "quark_folder_url"];
  const formFields = [...plainFields, ...numberFields, ...booleanFields, "telegram_api_id", "telegram_api_hash"];
  const state = {
    view: "download", data: null, tasks: new Map(), rows: new Map(), selected: new Set(), active: null,
    images: new Map(), brokenImages: new Set(), filters: {}, errors: new Map(), config: null,
    configLoaded: false, apiConfigured: false, apiIdConfigured: false, proxyProtected: false,
    configDirty: false, formRevision: 0, pending: false, logs: null, workbenchOK: false,
    lastSync: null, scroll: {}, browserRefreshPending: false, returnFocus: null
  };
  for (const view of Object.keys(views)) state.filters[view] = { query: "", status: "all", chat: "" };
  let workbenchRequest = null;
  let logsRequest = null;
  let configRequest = null;
  let pollTimer = null;
  let noticeTimer = null;

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const text = (id, value) => { const element = $(id); const next = String(value ?? ""); if (element.textContent !== next) element.textContent = next; };
  const finite = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const statusInfo = (task, upload = false) => (upload ? uploadStates[task.upload_status] : downloadStates[task.download_status]) || ["状态未知", "gray"];
  const taskName = (task) => String(task.title || task.filename || `素材 ${task.id}`);
  const providerName = (value) => ({ local: "本地模拟", quark_browser: "夸克网盘" }[value] || value || "未提供");
  const validationName = (task) => task.validation === "passed" ? "已校验" : task.validation === "pending" ? "待校验" : "校验状态未知";
  const allTasks = () => [...state.tasks.values()];
  const activeTask = () => state.tasks.get(state.active);
  const isUploadTask = (task) => task.download_status === "completed" || ["uploading", "link_pending", "completed", "local", "failed"].includes(task.upload_status);
  const canUpload = (task) => task.upload_status === "link_pending" || (task.download_status === "completed"
    && (["waiting", "failed"].includes(task.upload_status) || (task.upload_status === "local" && state.config?.upload_provider === "quark_browser")));
  const canRetry = (task) => retryable.has(task.download_status) && state.config?.telegram_access_mode === "api";
  const rescanHint = (task) => retryable.has(task.download_status) && state.config?.telegram_access_mode === "browser" ? "浏览器模式请重新扫描下载" : "";
  const selectedUploadTasks = () => [...state.selected].map((id) => state.tasks.get(id)).filter((task) => task && canUpload(task));
  const isBusy = () => state.pending || !state.workbenchOK || Boolean(state.data?.running);

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
    if (view === "library") return allTasks().filter((task) => task.download_status === "completed");
    return allTasks();
  }

  function matchesStatus(task, status) {
    if (status === "all") return true;
    if (state.view === "upload") {
      if (status === "waiting") return task.upload_status === "waiting" || task.upload_status === "local";
      if (status === "active") return task.upload_status === "uploading";
      if (status === "done") return task.upload_status === "completed";
      return ["failed", "link_pending"].includes(task.upload_status);
    }
    if (state.view === "library") {
      if (status === "pending") return task.validation !== "passed";
      if (status === "done") return task.validation === "passed";
      return Boolean(task.share_url);
    }
    if (status === "active") return ["queued", "downloading", "validating"].includes(task.download_status) || (state.view === "history" && task.upload_status === "uploading");
    if (status === "done") return task.download_status === "completed";
    return retryable.has(task.download_status) || (state.view === "history" && ["failed", "link_pending"].includes(task.upload_status));
  }

  function filteredTasks() {
    const filter = state.filters[state.view];
    const query = filter.query.trim().toLocaleLowerCase();
    return baseTasks().filter((task) => matchesStatus(task, filter.status)
      && (!filter.chat || String(task.chat ?? "") === filter.chat)
      && (!query || [task.title, task.filename, task.chat, task.id, task.message_id, task.local_path].some((value) => String(value ?? "").toLocaleLowerCase().includes(query))));
  }

  function progressMarkup(task, upload = false) {
    if (upload) {
      if (task.upload_status === "uploading") return `<div class="progress-label"><span>正在上传 / 等待网盘响应</span></div><div class="track indeterminate" role="progressbar" aria-label="上传进度未知"><span></span></div><div class="task-meta"><span>${escape(providerName(task.upload_provider))}</span><span>${escape(validationName(task))}</span></div>`;
      if (task.upload_status === "link_pending") return '<div class="task-meta"><span>结果待确认 / 链接待获取</span></div>';
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
    const key = JSON.stringify([url, image?.name, broken, large]);
    if (element.dataset.imageKey === key) return;
    element.dataset.imageKey = key;
    element.classList.add("image-slot");
    element.replaceChildren();
    if (!url || broken) {
      const fallback = document.createElement("span");
      fallback.className = "image-fallback";
      fallback.innerHTML = icon("file") + (large ? `<span>${broken ? "图片加载失败" : image?.url ? "图片地址不可用" : "暂无展示图"}</span>` : "");
      element.append(fallback);
      element.dataset.imageState = broken ? "failed" : "empty";
      icons(fallback);
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
    row.innerHTML = `<input class="task-checkbox" type="checkbox"><button type="button" class="thumb-button" data-task-action="inspect"></button><div class="task-body"><div class="title-line"><button type="button" class="task-title" data-task-action="inspect"></button><span class="badge"></span></div><div class="task-sub"></div><div class="task-progress"></div><p class="error-note" hidden></p><button type="button" class="text-button row-retry" data-task-action="retry" hidden>${icon("rotate-cw")}重新下载</button><button type="button" class="text-button row-link" data-task-action="link" hidden>${icon("link")}重新获取链接</button></div><button type="button" class="task-tail" data-task-action="inspect" title="查看详情" aria-label="查看详情">${icon("chevron-right")}</button>`;
    icons(row);
    return row;
  }

  function updateRow(row, task) {
    const id = String(task.id);
    const upload = state.view === "upload" || (state.view === "history" && task.download_status === "completed");
    const info = statusInfo(task, upload);
    row.classList.toggle("focused", state.active === id);
    row.classList.toggle("checked", state.selected.has(id));
    const checkbox = row.querySelector(".task-checkbox");
    checkbox.checked = state.selected.has(id);
    checkbox.setAttribute("aria-label", `选择 ${taskName(task)}`);
    const title = row.querySelector(".task-title");
    title.textContent = taskName(task);
    title.setAttribute("aria-pressed", String(state.active === id));
    const badge = row.querySelector(".badge");
    badge.textContent = info[0];
    badge.className = `badge ${info[1]}`;
    row.querySelector(".task-sub").textContent = [task.filename, task.chat].filter(Boolean).join(" · ") || "来源未提供";
    const thumb = row.querySelector(".thumb-button");
    thumb.setAttribute("aria-label", `查看 ${taskName(task)} 的详情`);
    imageSlot(thumb, task.images?.[0]);
    html(row.querySelector(".task-progress"), progressMarkup(task, upload));
    const error = row.querySelector(".error-note");
    error.hidden = !task.error && !rescanHint(task);
    error.textContent = [redact(task.error), rescanHint(task)].filter(Boolean).join("\n");
    const retry = row.querySelector(".row-retry");
    retry.hidden = !canRetry(task);
    retry.disabled = isBusy();
    const link = row.querySelector(".row-link");
    link.hidden = task.upload_status !== "link_pending";
    link.disabled = isBusy();
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
    const chats = [...new Set(baseTasks().map((task) => String(task.chat || "")).filter(Boolean))].sort();
    if (filter.chat && !chats.includes(filter.chat)) chats.push(filter.chat);
    const options = '<option value="">全部频道</option>' + chats.map((chat) => `<option value="${escape(chat)}">${escape(chat)}</option>`).join("");
    if ($("channel-filter").dataset.options !== options) {
      $("channel-filter").innerHTML = options;
      $("channel-filter").dataset.options = options;
      $("channel-filter").value = filter.chat;
    }
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
      text("empty-description", ({ download: state.config?.source_channel_url ? "当前来源尚无任务记录。" : "尚未配置来源频道。", upload: "已下载的素材会显示在这里。", library: "下载完成的素材会显示在这里。", history: "运行日志显示在下方。" })[state.view]);
    }
    text("result-count", state.data ? `${tasks.length} / ${baseTasks().length} 个素材` : "—");
    text("queue-status", state.view === "upload" ? "待校验素材将在上传前校验" : state.view === "library" ? "已下载文件" : state.data?.running ? "任务运行中" : state.data ? "当前空闲" : "状态未知");
    renderSelection(tasks);
    renderInspector();
  }

  function renderSelection(tasks = taskViews.has(state.view) ? filteredTasks() : []) {
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
    text("detail-chat", task.chat || "未提供"); text("detail-message", task.message_id ?? "未提供");
    const imageSources = {preceding_album: "前置图集（自动关联）", preceding_photo: "前置单图（自动关联）",
      document_thumbnail: "附件缩略图", zip: "ZIP 内图片"};
    text("detail-image-source", images.length ? imageSources[task.image_source] || "来源未记录" : "暂无图片");
    text("detail-image-messages", Array.isArray(task.image_message_ids) && task.image_message_ids.length ? task.image_message_ids.join(", ") : "无");
    text("detail-size", bytes(task.size)); text("detail-download", statusInfo(task)[0]);
    text("detail-validation", validationName(task)); text("detail-upload", statusInfo(task, true)[0]);
    text("detail-path", task.local_path || "暂无本地路径"); text("detail-provider", providerName(task.upload_provider));
    text("detail-updated", updated(task.updated_at));
    html($("detail-progress"), progressMarkup(task, state.view === "upload" || task.upload_status === "uploading" || task.upload_status === "link_pending"));
    text("detail-error", [redact(task.error), rescanHint(task)].filter(Boolean).join("\n")); $("detail-error").hidden = !task.error && !rescanHint(task);
    const source = externalURL(task.source_url);
    $("source-link").hidden = !source;
    if (source) $("source-link").href = source; else $("source-link").removeAttribute("href");
    const share = externalURL(task.share_url);
    $("share-result").hidden = !share;
    text("share-link", share);
    if (share) $("share-link").href = share; else $("share-link").removeAttribute("href");
    $("open-task").hidden = !task.local_path; $("open-task").disabled = state.pending || !state.workbenchOK;
    $("retry-task").hidden = !canRetry(task); $("retry-task").disabled = isBusy();
    $("retry-link").hidden = task.upload_status !== "link_pending"; $("retry-link").disabled = isBusy();
  }

  function renderMetrics() {
    const tasks = allTasks();
    let metrics;
    if (state.view === "upload") metrics = [
      ["待上传素材", tasks.filter((task) => task.download_status === "completed" && ["waiting", "local"].includes(task.upload_status)).length, "个已下载文件", ""],
      ["正在上传", tasks.filter((task) => task.upload_status === "uploading").length, "个任务", "green"],
      ["上传已完成", tasks.filter((task) => task.upload_status === "completed").length, "个素材", ""],
      ["需要处理", tasks.filter((task) => ["failed", "link_pending"].includes(task.upload_status)).length, "个任务", "amber"]
    ];
    else if (state.view === "library") metrics = [
      ["已下载素材", baseTasks().length, "个文件", ""], ["已校验", baseTasks().filter((task) => task.validation === "passed").length, "个文件", "green"],
      ["待校验", baseTasks().filter((task) => task.validation !== "passed").length, "个文件", "amber"], ["有分享链接", baseTasks().filter((task) => externalURL(task.share_url)).length, "个素材", ""]
    ];
    else {
      const downloading = tasks.filter((task) => task.download_status === "downloading");
      const speedKnown = downloading.length > 0 && downloading.every((task) => finite(task.speed));
      const received = tasks.filter((task) => finite(task.received));
      metrics = [["任务总数", tasks.length, "个素材文件", ""],
        ["实时下载速度", speedKnown ? `${bytes(downloading.reduce((sum, task) => sum + task.speed, 0))}/s` : "—", "", "green"],
        ["已接收数据", received.length ? bytes(received.reduce((sum, task) => sum + task.received, 0)) : "—", received.length && received.length !== tasks.length ? "已知接收量" : "", ""],
        ["需要处理", tasks.filter((task) => retryable.has(task.download_status) || (state.view === "history" && ["failed", "link_pending"].includes(task.upload_status))).length, "个任务", "amber"]];
    }
    html($("metrics"), metrics.map(([label, value, unit, tone]) => `<div class="metric ${tone}"><div class="metric-label">${label}</div><div class="metric-value">${escape(state.data ? value : "—")}<small>${unit}</small></div></div>`).join(""));
  }

  function renderHeader() {
    const data = state.data;
    const source = state.config?.source_channel_url;
    const subtitle = ({ download: source || "尚未配置来源频道", upload: data?.upload_target || "上传目标尚未读取", library: "本地已下载素材", history: "任务状态与运行日志", channels: "已配置来源与历史任务来源", settings: "本地配置" })[state.view];
    text("page-subtitle", state.configLoaded || state.view !== "download" ? subtitle : "来源配置尚未读取");
    text("download-source", source || (state.configLoaded ? "尚未配置来源频道" : "来源配置尚未读取"));
    text("upload-target", data?.upload_target || "未配置");
    const connectionLabel = !state.workbenchOK ? state.errors.has("workbench") ? "接口连接失败" : "正在读取状态" : data.running ? "任务运行中" : "工作台可用";
    $("connection").querySelector("span:last-child").textContent = connectionLabel;
    $("connection").querySelector(".status-dot").className = `status-dot ${!state.workbenchOK ? state.errors.has("workbench") ? "error" : "unknown" : data.running ? "busy" : ""}`;
    const actionLabels = { download: "下载", upload: "上传", "browser-check": "检查浏览器", "telegram-login": "Telegram 登录", "telegram-api-login": "Telegram API 登录", "quark-login": "夸克登录" };
    $("connection").title = data?.running && data.action ? actionLabels[String(data.action).replace(/^\/api\//, "")] || redact(data.action) : connectionLabel;
    $("download").hidden = state.view !== "download";
    $("download").disabled = isBusy() || !state.configLoaded;
    $("stop").hidden = !data?.running;
    $("stop").disabled = state.pending || !state.workbenchOK;
    $("open-downloads").hidden = !["download", "library", "settings"].includes(state.view);
    $("open-downloads").disabled = state.pending || (!data && !state.configLoaded);
    const cloudURL = externalURL(data?.upload_target);
    $("open-cloud").hidden = state.view !== "upload" || !cloudURL;
    if (cloudURL) $("open-cloud").href = cloudURL; else $("open-cloud").removeAttribute("href");
    $("upload").hidden = !["upload", "library"].includes(state.view);
    $("edit-channel").hidden = state.view !== "channels";
    $("destination").hidden = state.view !== "upload";
    $("download-settings").hidden = state.view !== "download";
    for (const id of ["save-config", "telegram-login", "quark-login", "browser-check"]) $(id).disabled = isBusy() || !state.configLoaded;
    $("quark-login").hidden = $("upload_provider").value !== "quark_browser";
    $("config-fields").disabled = !state.configLoaded || state.pending;
    text("nav-download", data ? state.tasks.size : "—");
    text("nav-upload", data ? allTasks().filter(isUploadTask).length : "—");
    text("nav-library", data ? allTasks().filter((task) => task.download_status === "completed").length : "—");
    const disk = data?.disk;
    const diskKnown = disk && finite(disk.free) && finite(disk.total) && disk.total > 0 && disk.free <= disk.total;
    $("disk-track").hidden = !diskKnown;
    if (diskKnown) {
      const used = (disk.total - disk.free) / disk.total * 100;
      text("disk-percent", `${Math.round(used)}%`);
      $("disk-track").firstElementChild.style.width = `${used}%`;
    } else text("disk-percent", "—");
    text("disk-space", disk && finite(disk.free) ? `可用空间 ${bytes(disk.free)}` : "存储信息不可用");
    text("access-mode", state.configLoaded ? state.config.telegram_access_mode === "api" ? "Telegram API 模式" : "Telegram Web 模式" : "获取方式未读取");
    text("footer-context", state.configLoaded ? `本地工作空间 / ${state.config.telegram_access_mode === "api" ? "Telegram API" : "Telegram Web"}` : "本地工作空间");
    text("last-sync", state.lastSync ? `${state.workbenchOK ? "最近同步" : "上次成功同步"} ${state.lastSync.toLocaleTimeString("zh-CN", { hour12: false })}` : "尚未同步");
    renderSelection();
  }

  function renderChannels() {
    const source = state.config?.source_channel_url;
    html($("configured-channel"), `<div class="channel-row"><div><h3>${source ? "已配置来源" : state.configLoaded ? "尚未配置来源频道" : "来源配置尚未读取"}</h3><p>${escape(source || "")}</p></div><div class="channel-actions">${externalURL(source) ? `<a class="button" href="${escape(externalURL(source))}" target="_blank" rel="noopener noreferrer">${icon("external-link")}打开频道</a>` : ""}<button type="button" data-go-settings="source_channel_url">${icon("pencil")}编辑来源</button></div></div>`);
    const chats = new Map();
    for (const task of allTasks()) if (task.chat) chats.set(String(task.chat), (chats.get(String(task.chat)) || 0) + 1);
    html($("channel-list"), chats.size ? [...chats].map(([chat, count]) => `<div class="channel-row"><div><h3>${escape(chat)}</h3><p>${count} 个素材</p></div><button type="button" data-channel="${escape(chat)}">查看任务${icon("chevron-right")}</button></div>`).join("") : '<div class="empty-state compact"><p>暂无来源记录</p></div>');
  }

  function render() {
    renderHeader();
    if (taskViews.has(state.view)) { renderMetrics(); renderList(); }
    if (state.view === "channels") renderChannels();
  }

  function setView(view, push = true) {
    if (!views[view]) view = "download";
    state.scroll[state.view] = window.scrollY;
    state.view = view;
    closeInspector();
    if (push) { const url = new URL(location.href); url.searchParams.set("view", view); history.pushState({}, "", url); }
    document.title = `${views[view]} · 素材工作台`;
    text("breadcrumb", views[view]); text("page-title", views[view]);
    for (const link of document.querySelectorAll(".nav-item")) {
      link.classList.toggle("active", link.dataset.view === view);
      if (link.dataset.view === view) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    $("metrics").hidden = !taskViews.has(view);
    $("task-workspace").hidden = !taskViews.has(view);
    $("logs-section").hidden = view !== "history";
    $("settings-section").hidden = view !== "settings";
    $("channels-section").hidden = view !== "channels";
    $("task-search").value = state.filters[view].query;
    $("channel-filter").value = state.filters[view].chat;
    render();
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
        state.workbenchOK = true;
        state.lastSync = new Date();
        for (const id of state.selected) if (!tasks.has(id)) state.selected.delete(id);
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

  async function refreshAll() { await Promise.all([refreshWorkbench(), refreshLogs()]); }

  async function poll() {
    clearTimeout(pollTimer);
    await refreshAll();
    pollTimer = setTimeout(poll, document.hidden ? 10000 : 2000);
  }

  function markRequired() {
    const api = $("telegram_access_mode").value === "api";
    const quark = $("upload_provider").value === "quark_browser";
    const required = new Set(["recent_message_limit", "download_start_timeout_seconds", "download_transfer_timeout_seconds", "inbox_dir"]);
    if (api) { if (!state.apiIdConfigured) required.add("telegram_api_id"); if (!state.apiConfigured) required.add("telegram_api_hash"); }
    else required.add("telegram_browser_timeout_seconds");
    if (!api || quark) required.add("browser_executable_path");
    for (const id of formFields) {
      $(id).required = required.has(id);
      $(id).setAttribute("aria-required", String(required.has(id)));
    }
    $("api-fields").hidden = !api;
    $("browser-path-field").hidden = api && !quark;
    $("browser-timeout-field").hidden = api;
    $("telegram-headless-field").hidden = api;
    $("browser-check").hidden = api && !quark;
    $("quark-fields").hidden = !quark;
    $("telegram_run_mode").querySelector('[value="watch"]').disabled = api;
    if (api) $("telegram_run_mode").value = "once";
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
        if (!["browser", "api"].includes(config.telegram_access_mode) || !["local", "quark_browser"].includes(config.upload_provider)) throw new Error("/api/config：缺少有效获取方式或上传方式");
        // Credentials from a legacy server are reduced to flags, never stored or echoed.
        state.apiConfigured = config.telegram_api_configured === true;
        state.apiIdConfigured = config.telegram_api_id_configured === true || (Number.isInteger(config.telegram_api_id) && config.telegram_api_id > 0);
        state.config = {};
        for (const id of [...plainFields, ...numberFields, ...booleanFields, "download_dir", "unverified_dir", "log_path"]) if (config[id] !== undefined) state.config[id] = config[id];
        state.proxyProtected = /:\/\/[^\s/@]+@/.test(String(config.telegram_api_proxy_url || ""));
        if (state.proxyProtected) state.config.telegram_api_proxy_url = "";
        state.configLoaded = true;
        if (revision === state.formRevision) {
          for (const id of [...plainFields, ...numberFields]) {
            const input = $(id);
            const value = state.config[id];
            if (input instanceof HTMLSelectElement && value != null && ![...input.options].some((option) => option.value === String(value))) input.add(new Option(String(value), String(value)));
            if (value !== undefined) input.value = String(value ?? "");
          }
          for (const id of booleanFields) $(id).checked = state.config[id] === true;
          $("telegram_api_id").value = ""; $("telegram_api_hash").value = "";
          state.configDirty = false;
          text("config-dirty", "");
        }
        text("config-download-dir", config.download_dir || "未提供");
        text("config-unverified-dir", config.unverified_dir || "未提供");
        text("config-log-path", config.log_path || "未提供");
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
    const directory = $("inbox_dir").value.trim();
    if (!directory) return failField("inbox_dir", "请填写下载保存目录");
    if (!/^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+\\)/.test(directory)) return failField("inbox_dir", "请填写完整的绝对目录路径");
    const api = $("telegram_access_mode").value === "api";
    const quark = $("upload_provider").value === "quark_browser";
    for (const id of numberFields) {
      if (api && id === "telegram_browser_timeout_seconds" && !$(id).value) continue;
      const value = Number($(id).value);
      if (!$(id).value || !Number.isInteger(value) || value < Number($(id).min) || value > Number($(id).max)) return failField(id, `请填写 ${$(id).min} 到 ${$(id).max} 的整数：${$(id).closest("label").firstChild.textContent.trim()}`);
    }
    if ((!api || quark || purpose === "browser" || purpose === "quark") && !$("browser_executable_path").value.trim()) return failField("browser_executable_path", "请填写浏览器程序路径");
    const id = $("telegram_api_id").value.trim();
    if ((id || (api && !state.apiIdConfigured)) && (!/^\d+$/.test(id) || Number(id) < 1 || Number(id) > 2147483647)) return failField("telegram_api_id", "Telegram API ID 必须为有效正整数");
    const hash = $("telegram_api_hash").value.trim();
    if ((hash || (api && !state.apiConfigured)) && !/^[a-f0-9]{32}$/i.test(hash)) return failField("telegram_api_hash", "Telegram API Hash 必须为 32 位十六进制字符");
    const source = $("source_channel_url").value.trim();
    if ((source || purpose === "download" || (purpose === "telegram" && !api)) && !/^https:\/\/t\.me\/[A-Za-z0-9_]+\/?$/.test(source)) return failField("source_channel_url", "请填写有效的 Telegram 公开频道链接");
    const folder = $("quark_folder_url").value.trim();
    if ((folder || purpose === "quark" || (purpose === "upload" && quark)) && !externalURL(folder)) return failField("quark_folder_url", "请填写有效的夸克目标文件夹 URL");
    return true;
  }

  function values() {
    const output = {};
    for (const id of plainFields) output[id] = $(id).value.trim();
    for (const id of numberFields) if ($(id).value) output[id] = Number($(id).value);
    for (const id of booleanFields) output[id] = $(id).checked;
    if (state.proxyProtected && !output.telegram_api_proxy_url) delete output.telegram_api_proxy_url;
    const id = $("telegram_api_id").value.trim();
    const hash = $("telegram_api_hash").value.trim();
    if (id) output.telegram_api_id = Number(id);
    if (hash) output.telegram_api_hash = hash;
    // Omit non-form fields so advanced configuration remains unchanged.
    return output;
  }

  async function saveConfig() {
    const payload = values();
    await post("/api/config", payload);
    if (payload.telegram_api_hash) state.apiConfigured = true;
    if (payload.telegram_api_id) state.apiIdConfigured = true;
    $("telegram_api_id").value = ""; $("telegram_api_hash").value = "";
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

  async function copy(value, label) {
    try { if (!navigator.clipboard) throw new Error("当前环境不支持剪贴板"); await navigator.clipboard.writeText(value); notice(`${label}已复制`); }
    catch (error) { setError("action", `复制失败：${error.message}`); }
  }

  document.addEventListener("click", (event) => {
    const nav = event.target.closest("a[data-view]");
    if (nav && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); setView(nav.dataset.view); return; }
    const settings = event.target.closest("[data-go-settings]");
    if (settings) { goSettings(settings.dataset.goSettings); return; }
    const channel = event.target.closest("[data-channel]");
    if (channel) { state.filters.download.chat = channel.dataset.channel; state.filters.download.status = "all"; state.filters.download.query = ""; setView("download"); return; }
    const tab = event.target.closest("[data-filter]");
    if (tab) { state.filters[state.view].status = tab.dataset.filter; renderList(); return; }
    const retry = event.target.closest("[data-retry]");
    if (retry) {
      const actions = { workbench: refreshWorkbench, logs: refreshLogs, config: loadConfig, "browser-info": refreshBrowserInfo, images: () => {
        state.brokenImages.clear(); setError("images", "");
        $("preview-strip").dataset.gallery = "";
        renderList();
      } };
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
  });
  $("task-list").addEventListener("change", (event) => {
    if (!event.target.matches(".task-checkbox")) return;
    const id = event.target.closest("[data-task-id]").dataset.taskId;
    if (event.target.checked) state.selected.add(id); else state.selected.delete(id);
    event.target.closest(".task").classList.toggle("checked", event.target.checked);
    renderSelection();
  });
  $("select-all").addEventListener("change", () => {
    for (const task of filteredTasks()) { if ($("select-all").checked) state.selected.add(String(task.id)); else state.selected.delete(String(task.id)); }
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
  $("upload").addEventListener("click", () => void uploadTasks(selectedUploadTasks()));
  $("retry-link").addEventListener("click", () => { if (activeTask()?.upload_status === "link_pending") void uploadTasks([activeTask()]); });
  $("retry-task").addEventListener("click", () => void retryTask(activeTask()));
  $("stop").addEventListener("click", () => void mutation(async () => { await post("/api/stop"); notice("已请求停止，等待当前任务响应"); }));
  $("open-downloads").addEventListener("click", () => void mutation(async () => { await post("/api/open-downloads"); notice("已请求打开下载目录"); }));
  $("open-task").addEventListener("click", () => {
    const task = activeTask();
    if (task) void mutation(async () => { await post("/api/open-task", { id: task.id }); notice("已请求打开本地目录"); });
  });
  $("copy-share").addEventListener("click", () => { const url = externalURL(activeTask()?.share_url); if (url) void copy(url, "分享链接"); });
  $("copy-logs").addEventListener("click", () => { if (state.logs) void copy(state.logs.join("\n"), "日志"); });
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
  for (const id of ["telegram_access_mode", "upload_provider"]) $(id).addEventListener("change", markRequired);
  $("config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (isBusy() || !state.configLoaded || !validate()) return;
    void mutation(async () => { if (await saveConfig()) notice("配置已保存"); });
  });
  $("browser-check").addEventListener("click", () => void configuredAction("/api/browser-check", "browser", "已请求检查浏览器"));
  $("telegram-login").addEventListener("click", () => void configuredAction($("telegram_access_mode").value === "api" ? "/api/telegram-api-login" : "/api/telegram-login", "telegram", "已请求打开 Telegram 登录"));
  $("quark-login").addEventListener("click", () => void configuredAction("/api/quark-login", "quark", "已请求打开夸克登录"));
  window.addEventListener("popstate", () => setView(new URLSearchParams(location.search).get("view"), false));
  window.addEventListener("beforeunload", (event) => { if (state.configDirty) { event.preventDefault(); event.returnValue = ""; } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void refreshAll(); });

  icons();
  if (!window.lucide) setError("icons", "本地图标库加载失败：/static/lucide.js");
  setView(new URLSearchParams(location.search).get("view"), false);
  void loadConfig();
  void poll();
})();
