/* Product editing owns its form state; background polling never replaces unsaved input. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const escape = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (v) => `<i data-lucide="${v}" aria-hidden="true"></i>`;
  const labels = { editing: "待完善", ready: "待生成", saving: "保存中", saved: "已保存", attention: "需处理", pending: "结果待确认" };
  const fields = ["title", "price", "stock", "category", "format", "variant", "description"];
  const templateFields = { shop: "shop_name", price: "default_price", stock: "default_stock", category: "default_category",
    format: "default_format", variant: "default_variant", description: "default_description", url: "publish_url" };
  let api;
  const state = { rows: [], template: {}, selected: new Set(), filter: "all", active: "", form: null,
    dirty: false, pending: false, request: null, image: 0, error: "", fetched: false, shop: {} };
  const icons = () => window.lucide?.createIcons();
  function markup(id, value) {
    const el = $(id);
    if (el.dataset.markup !== value) { el.innerHTML = value; el.dataset.markup = value; icons(); }
  }
  function note(id, value) { $(id).textContent = value; $(id).hidden = !value; }
  function safeURL(value, local = false) {
    if (local) return /^\/api\/(?:product-drafts\/[a-f0-9]{24}\/images\/\d+|synced-image\/[a-f0-9]{64}\.(?:jpg|png|webp|gif))$/.test(value || "") ? value : "";
    try { const u = new URL(value); return u.protocol === "https:" && !u.username && !u.password ? u.href : ""; } catch { return ""; }
  }
  const activeRow = () => state.rows.find((r) => r.id === state.active);
  const readonly = () => !state.form || state.form.remote_device || ["saving", "saved", "pending"].includes(state.form.status) || Boolean(state.form.remote_url);
  const busy = () => state.pending || api.isBusy();
  function visibleRows() {
    const query = $("draft-search").value.trim().toLocaleLowerCase();
    return state.rows.filter((r) => (!query || `${r.title} ${r.filename} ${r.message_id} ${r.shop_name}`.toLocaleLowerCase().includes(query))
      && (state.filter === "all" || state.filter === "ready" && ["editing", "ready"].includes(r.status)
        || state.filter === "attention" && ["attention", "pending"].includes(r.status) || state.filter === r.status));
  }
  function editorValues() {
    const output = {};
    for (const f of fields) output[f] = f === "stock" ? Number($(`draft-${f}`).value) : $(`draft-${f}`).value.trim();
    output.image_indices = [...state.form.image_indices];
    return output;
  }
  function localMissing() {
    if (!state.form) return [];
    const values = editorValues();
    return [["title", "商品标题"], ["price", "商品价格"], ["category", "商品类目"], ["description", "商品详情"]]
      .filter(([f]) => !values[f]).map(([, label]) => label).concat(values.image_indices.length ? [] : ["商品图片"]);
  }
  function setForm(row) {
    state.active = row?.id || "";
    state.form = row ? structuredClone(row) : null;
    state.dirty = false;
    state.image = row?.image_indices?.[0] ?? 0;
    for (const f of fields) $(`draft-${f}`).value = row?.[f] ?? "";
  }
  async function choose(id) {
    if (state.active === id) return;
    await saveLocal();
    setForm(state.rows.find((r) => r.id === id));
    render();
  }
  async function refresh() {
    if (state.request) return state.request;
    state.request = (async () => {
      try {
        const data = await api.request("/api/drafts");
        if (!Array.isArray(data.drafts) || !data.template) throw new Error("商品草稿接口返回格式无效");
        state.rows = data.drafts;
        state.template = data.template;
        state.shop = data.qianniu_shop || {};
        state.fetched = true;
        state.error = "";
        for (const id of state.selected) if (!state.rows.some((r) => r.id === id && !r.remote_device && !["saving", "saved", "pending"].includes(r.status))) state.selected.delete(id);
        const row = activeRow();
        if (!state.form) setForm(row || state.rows[0]);
        else if (!state.dirty && row && row.revision !== state.form.revision) setForm(row);
      } catch (error) { state.error = error.message; }
      finally { render(); }
    })();
    try { await state.request; } finally { state.request = null; }
  }
  async function saveLocal() {
    if (!state.form || !state.dirty) return;
    if (readonly()) throw new Error("该草稿已提交千牛，不能修改本地资料");
    if (!$("draft-editor-form").reportValidity()) throw new Error("请检查商品资料的格式");
    await api.post("/api/drafts/save", { id: state.active, revision: state.form.revision, values: editorValues() });
    state.dirty = false;
    if (state.request) await state.request;
    await refresh();
  }
  async function perform(operation, errorId = "draft-error") {
    if (state.pending) return;
    state.pending = true; note(errorId, ""); render();
    try { await operation(); }
    catch (error) { note(errorId, error.message.replace(/^\/api\/[^：:\s]+[:：]\s*/, "")); }
    finally { state.pending = false; await refresh(); render(); }
  }
  async function publish(ids) {
    await saveLocal();
    const rows = ids.map((id) => state.rows.find((r) => r.id === id));
    if (rows.some((r) => !r || r.remote_device || ["saving", "saved", "pending"].includes(r.status))) throw new Error("请选择尚未提交千牛的本机资料");
    await api.post("/api/drafts/publish", { entries: rows.map((r) => ({ id: r.id, revision: r.revision })) });
    api.notice("已开始填写并保存千牛草稿");
    await api.refreshWorkbench();
  }
  function renderEditor() {
    const row = state.form;
    $("draft-editor-empty").hidden = Boolean(row);
    $("draft-editor-form").hidden = !row;
    if (!row) return;
    const latest = activeRow();
    const conflict = state.dirty && latest && latest.revision !== row.revision;
    $("draft-identity").textContent = `${row.shop_name || "待绑定店铺"} · ${row.id.slice(0, 8)}`;
    $("draft-status").textContent = `${labels[latest?.status || row.status] || row.status}${row.remote_device ? " · 其他设备" : ""}`;
    $("draft-status").className = `badge ${["pending", "attention", "editing"].includes(latest?.status) ? "amber" : ""}`;
    const stages = ["上传夸克", "整理资料", "填写千牛", "草稿保存"];
    markup("draft-stages", stages.map((label, i) => `<li class="${i === 0 || i === 1 && !(row.missing || []).length || row.status === "saved" ? "done" : i === 2 && row.status === "saving" ? "current" : ""}"><span>${i + 1}</span>${label}</li>`).join(""));
    $("draft-source-file").textContent = row.filename;
    $("draft-source-message").textContent = row.message_id > 0 ? `${row.chat} · 消息 #${row.message_id}` : "本地素材";
    $("draft-shop").textContent = `目标店铺：${row.shop_name || "尚未绑定"}`;
    $("draft-delivery-link").href = safeURL(row.share_url);
    $("draft-title-count").textContent = `${$("draft-title").value.length} / 60`;
    for (const f of fields) $(`draft-${f}`).disabled = readonly() || state.pending;
    const images = row.images || [];
    const current = images.find((image) => image.index === state.image);
    const hero = $("draft-hero-image");
    const url = safeURL(current?.url, true);
    if (hero.getAttribute("src") !== url) { if (url) hero.src = url; else hero.removeAttribute("src"); }
    hero.hidden = !url;
    $("draft-hero").disabled = !url;
    $("draft-hero").classList.toggle("has-image", Boolean(url));
    $("draft-image-count").textContent = `已选 ${row.image_indices.length} / ${images.length}`;
    markup("draft-image-list", images.map((image) => {
      const selected = row.image_indices.indexOf(image.index);
      return `<div class="draft-thumbnail${image.index === state.image ? " active" : ""}"><button type="button" data-draft-photo="${image.index}" title="查看第 ${image.index + 1} 张" aria-label="查看第 ${image.index + 1} 张"><img src="${escape(safeURL(image.url, true))}" alt="商品图 ${image.index + 1}" loading="lazy"></button><label><input type="checkbox" data-draft-image="${image.index}" ${selected >= 0 ? "checked" : ""} ${readonly() || state.pending ? "disabled" : ""}>${selected === 0 ? "主图" : selected > 0 ? selected + 1 : "选用"}</label></div>`;
    }).join(""));
    $("draft-set-cover").disabled = readonly() || state.pending || !images.length || row.image_indices[0] === state.image;
    const missing = localMissing();
    $("draft-validation").textContent = missing.length ? `待补充：${missing.join("、")}` : row.shop_name ? "商品资料已就绪" : "商品资料已就绪 · 待绑定店铺";
    $("draft-validation").classList.toggle("incomplete", Boolean(missing.length));
    note("draft-record-error", latest?.error || "");
    $("draft-receipt").textContent = row.saved_at ? `${row.confirmed_by === "manual" ? "人工核对已记录" : "千牛保存回执已确认"} · ${new Date(row.saved_at).toLocaleString("zh-CN")}` : "";
    $("draft-save-note").textContent = conflict ? "资料已变化，请重新读取后编辑" : state.dirty ? "有未保存的修改" : row.remote_device ? "其他设备的草稿，仅可查看" : "资料已保存至工作台";
    $("draft-reload").hidden = !conflict;
    $("draft-save-local").disabled = readonly() || state.pending || conflict || !state.dirty;
    $("draft-publish").disabled = readonly() || busy() || conflict || Boolean(missing.length) || !row.shop_name;
    $("draft-export").href = row.remote_device ? "" : `/api/product-drafts/${row.id}/export`;
    $("draft-export").hidden = row.remote_device;
    $("draft-export").setAttribute("download", "product-draft.zip");
    $("draft-remote").hidden = !safeURL(row.remote_url);
    if (safeURL(row.remote_url)) $("draft-remote").href = safeURL(row.remote_url); else $("draft-remote").removeAttribute("href");
    $("draft-confirm").hidden = row.remote_device || row.status !== "pending";
    $("draft-confirm").disabled = busy();
  }
  function renderShop() {
    const shop = state.shop;
    const messages = {
      starting: "正在打开千牛",
      waiting: "等待在千牛窗口中完成登录",
      loading: "正在加载千牛页面",
      recognizing: `正在识别店铺${Number.isFinite(shop.remaining_seconds) ? `（剩余 ${shop.remaining_seconds} 秒）` : ""}`,
      detected: "店铺已识别，待确认",
      confirmed: "店铺已确认并保存",
      cancelled: "店铺识别已停止",
      failed: shop.error || "店铺识别未完成"
    };
    const summary = messages[shop.stage] || (state.template.qianniu_shop_name ? `当前店铺：${state.template.qianniu_shop_name}` : "尚未识别店铺");
    $("dt-shop-status").textContent = summary;
    $("draft-shop-settings-status").textContent = summary;
    const available = shop.stage === "detected" && shop.candidates?.length;
    $("dt-shop-result").hidden = !available;
    if (available) {
      const ticket = shop.ticket + JSON.stringify(shop.candidates);
      if ($("dt-shop-candidate").dataset.ticket !== ticket) {
        $("dt-shop-candidate").innerHTML = shop.candidates.map((row, index) =>
          `<option value="${index}">${escape(row.name)}${row.shop_id ? ` · ${escape(row.shop_id)}` : ""}</option>`).join("");
        $("dt-shop-candidate").dataset.ticket = ticket;
      }
    }
    for (const id of ["draft-login", "dt-shop-detect", "draft-login-settings", "dt-shop-use"]) $(id).disabled = busy();
    $("dt-save").disabled = busy();
  }
  function render() {
    if (!api) return;
    const shown = document.body.dataset.view === "drafts";
    $("product-drafts-section").hidden = !shown;
    $("draft-open-template").hidden = !shown;
    $("draft-import").hidden = !shown;
    $("nav-drafts").textContent = state.fetched ? state.rows.length : "—";
    renderShop();
    if (!shown) return;
    $("draft-auto-create").checked = state.template.qianniu_auto_create_draft === true;
    $("draft-auto-create").disabled = busy() || !state.fetched;
    $("draft-auto-summary").textContent = state.template.qianniu_auto_save_draft ? "夸克上传完成 → 整理资料 → 保存千牛草稿" : "夸克上传完成后整理商品资料";
    if (state.error) note("draft-error", state.error);
    markup("draft-tabs", [["all", "全部"], ["ready", "待生成"], ["saving", "保存中"], ["saved", "已保存"], ["attention", "需处理"]].map(([status, title]) => {
      const count = state.rows.filter((r) => status === "all" || status === "ready" && ["editing", "ready"].includes(r.status) || status === "attention" && ["pending", "attention"].includes(r.status) || r.status === status).length;
      return `<button type="button" data-draft-filter="${status}" aria-pressed="${state.filter === status}">${title}<span>${count}</span></button>`;
    }).join(""));
    const rows = visibleRows();
    markup("draft-list", rows.map((row) => `<div class="draft-row${row.id === state.active ? " active" : ""}"><input type="checkbox" data-draft-select="${escape(row.id)}" aria-label="选择 ${escape(row.title)}" ${state.selected.has(row.id) ? "checked" : ""} ${row.remote_device || ["saving", "saved", "pending"].includes(row.status) ? "disabled" : ""}><button class="draft-row-open" type="button" data-draft-open="${escape(row.id)}">${row.images?.length ? `<img src="${escape(safeURL(row.images.find((im) => im.index === row.image_indices[0])?.url || row.images[0]?.url, true))}" alt="" loading="lazy">` : `<span class="draft-row-placeholder">${icon("image")}</span>`}<span class="draft-row-copy"><strong>${escape(row.title || row.filename)}</strong><small>${escape(row.shop_name || "待绑定店铺")} · #${escape(row.message_id || "本地")}</small><span class="draft-row-status ${["attention", "pending"].includes(row.status) ? "attention" : ""}">${escape(labels[row.status] || row.status)}</span></span>${icon("chevron-right")}</button></div>`).join(""));
    $("draft-list-count").textContent = `${rows.length} 条`;
    $("draft-list-empty").hidden = Boolean(rows.length);
    $("draft-selection-count").textContent = `已选 ${state.selected.size} 项`;
    const selectable = rows.filter((r) => !r.remote_device && !["saving", "saved", "pending"].includes(r.status));
    $("draft-select-all").checked = Boolean(selectable.length) && selectable.every((r) => state.selected.has(r.id));
    $("draft-select-all").indeterminate = selectable.some((r) => state.selected.has(r.id)) && !$("draft-select-all").checked;
    $("draft-publish-selected").disabled = busy() || state.selected.size === 0;
    $("draft-import").disabled = busy() || !state.fetched;
    $("draft-open-template").disabled = state.pending || !state.fetched;
    renderEditor();
  }
  async function openTemplate() {
    await refresh();
    if (!state.fetched) throw new Error("发布模板尚未读取");
    for (const [id, key] of Object.entries(templateFields)) $(`dt-${id}`).value = state.template[`qianniu_${key}`] ?? "";
    $("dt-auto-create").checked = Boolean(state.template.qianniu_auto_create_draft);
    $("dt-auto-save").checked = Boolean(state.template.qianniu_auto_save_draft);
    $("dt-selectors").value = JSON.stringify(state.template.qianniu_form_selectors || {}, null, 2);
    note("dt-error", "");
    $("draft-template-dialog").showModal();
    renderShop();
  }
  function init(bridge) {
    api = bridge;
    for (const f of fields) $(`draft-${f}`).addEventListener("input", () => { state.dirty = true; renderEditor(); });
    $("draft-editor-form").addEventListener("submit", (event) => {
      event.preventDefault(); void perform(async () => { await saveLocal(); api.notice("商品资料已保存"); });
    });
    $("draft-search").addEventListener("input", render);
    $("draft-tabs").addEventListener("click", (event) => { const b = event.target.closest("[data-draft-filter]"); if (b) { state.filter = b.dataset.draftFilter; render(); } });
    $("draft-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-draft-open]");
      if (button) void perform(() => choose(button.dataset.draftOpen));
    });
    $("draft-list").addEventListener("change", (event) => {
      const id = event.target.dataset.draftSelect;
      if (id) { if (event.target.checked) state.selected.add(id); else state.selected.delete(id); render(); }
    });
    $("draft-select-all").addEventListener("change", () => {
      for (const r of visibleRows().filter((r) => !r.remote_device && !["saving", "saved", "pending"].includes(r.status))) {
        if ($("draft-select-all").checked) state.selected.add(r.id); else state.selected.delete(r.id);
      }
      render();
    });
    $("draft-image-list").addEventListener("click", (event) => { const b = event.target.closest("[data-draft-photo]"); if (b) { state.image = Number(b.dataset.draftPhoto); renderEditor(); } });
    $("draft-image-list").addEventListener("change", (event) => {
      if (!event.target.matches("[data-draft-image]") || readonly()) return;
      const index = Number(event.target.dataset.draftImage);
      if (event.target.checked && state.form.image_indices.length >= 5) { event.target.checked = false; note("draft-error", "商品主图最多选择 5 张"); return; }
      state.form.image_indices = event.target.checked ? [...state.form.image_indices, index] : state.form.image_indices.filter((i) => i !== index);
      state.dirty = true; renderEditor();
    });
    $("draft-set-cover").addEventListener("click", () => {
      state.form.image_indices = [state.image, ...state.form.image_indices.filter((i) => i !== state.image)].slice(0, 5);
      state.dirty = true; renderEditor();
    });
    $("draft-hero").addEventListener("click", () => {
      $("draft-large-image").src = $("draft-hero-image").src; $("draft-image-dialog").showModal();
    });
    for (const b of document.querySelectorAll("[data-draft-close]")) b.addEventListener("click", () => b.closest("dialog").close());
    $("draft-reload").addEventListener("click", () => { setForm(activeRow()); renderEditor(); });
    $("draft-publish").addEventListener("click", () => void perform(() => publish([state.active])));
    $("draft-publish-selected").addEventListener("click", () => void perform(() => publish([...state.selected])));
    for (const id of ["draft-open-template", "draft-settings-template"]) $(id).addEventListener("click", () => void perform(openTemplate));
    const login = async () => {
      if (api.isBusy()) throw new Error("请等待当前任务结束");
      let selectors;
      try { selectors = JSON.parse($("dt-selectors").value || "{}"); } catch { throw new Error("页面控件须为有效 JSON"); }
      if (!selectors || Array.isArray(selectors) || typeof selectors !== "object") throw new Error("页面控件须为 JSON 对象");
      await api.post("/api/qianniu/login", { url: $("dt-url").value.trim(), shop_selector: selectors.shop || "" });
      api.notice("已启动千牛登录与店铺识别");
      await api.refreshWorkbench();
    };
    $("draft-login-settings").addEventListener("click", () => void perform(async () => { await openTemplate(); await login(); }, "dt-error"));
    for (const id of ["draft-login", "dt-shop-detect"]) $(id).addEventListener("click", () => void perform(login, "dt-error"));
    $("dt-shop-use").addEventListener("click", () => void perform(async () => {
      await saveLocal();
      const result = await api.post("/api/qianniu/shop/confirm", { ticket: state.shop.ticket, index: Number($("dt-shop-candidate").value) });
      $("dt-shop").value = result.shop_name;
      api.templateSaved({ qianniu_shop_name: result.shop_name });
      api.notice(result.message);
    }, "dt-error"));
    $("draft-template-form").addEventListener("submit", (event) => {
      event.preventDefault();
      void perform(async () => {
        const value = {};
        for (const [id, key] of Object.entries(templateFields)) value[`qianniu_${key}`] = id === "stock" ? Number($(`dt-${id}`).value) : $(`dt-${id}`).value.trim();
        value.qianniu_auto_create_draft = $("dt-auto-create").checked;
        value.qianniu_auto_save_draft = $("dt-auto-save").checked;
        try { value.qianniu_form_selectors = JSON.parse($("dt-selectors").value || "{}"); } catch { throw new Error("页面控件须为有效 JSON"); }
        await api.post("/api/drafts/template", value);
        api.templateSaved(value); $("draft-template-dialog").close(); api.notice("发布模板已保存");
      }, "dt-error");
    });
    $("dt-auto-save").addEventListener("change", () => { if ($("dt-auto-save").checked) $("dt-auto-create").checked = true; });
    $("dt-auto-create").addEventListener("change", () => { if (!$("dt-auto-create").checked) $("dt-auto-save").checked = false; });
    $("draft-auto-create").addEventListener("change", () => {
      const checked = $("draft-auto-create").checked;
      state.template.qianniu_auto_create_draft = checked;
      if (!checked) state.template.qianniu_auto_save_draft = false;
      void perform(async () => {
        await api.post("/api/drafts/template", { qianniu_auto_create_draft: checked, ...(!checked ? { qianniu_auto_save_draft: false } : {}) });
        api.notice(checked ? "已开启上传后整理资料" : "已关闭自动草稿");
      });
    });
    $("draft-import").addEventListener("click", () => {
      const tasks = api.getTasks().filter((t) => !t.remote_device && !t.record_removed_at && t.upload_status === "completed" && t.share_url && ["quark_api", "quark_browser", "quark_cli"].includes(t.upload_provider));
      markup("draft-import-list", tasks.map((task) => `<label class="draft-import-row"><input type="checkbox" value="${escape(task.id)}"><span><strong>${escape(task.remote_filename || task.filename)}</strong><small>${escape(task.chat || "本地素材")} · #${escape(task.message_id || "本地")}</small></span></label>`).join(""));
      $("draft-import-note").textContent = tasks.length ? `可添加 ${tasks.length} 个已上传素材，已有草稿会保留` : "暂无已上传并取得分享链接的本机素材";
      $("draft-import-confirm").disabled = !tasks.length; note("draft-import-error", ""); $("draft-import-dialog").showModal();
    });
    $("draft-import-confirm").addEventListener("click", () => void perform(async () => {
      const ids = [...$("draft-import-list").querySelectorAll("input:checked")].map((el) => el.value);
      if (!ids.length) throw new Error("请选择素材");
      await saveLocal();
      const result = await api.post("/api/drafts/create", { ids });
      $("draft-import-dialog").close(); await refresh(); setForm(state.rows.find((r) => r.id === result.ids[0])); api.notice(result.message);
    }, "draft-import-error"));
    $("draft-confirm").addEventListener("click", () => {
      $("draft-confirm-url").value = ""; $("draft-confirm-checked").checked = false; note("draft-confirm-error", ""); $("draft-confirm-dialog").showModal();
    });
    $("draft-confirm-form").addEventListener("submit", (event) => {
      event.preventDefault();
      void perform(async () => {
        await api.post("/api/drafts/confirm", { id: state.active, revision: state.form.revision, confirm: $("draft-confirm-checked").checked, remote_url: $("draft-confirm-url").value.trim() });
        $("draft-confirm-dialog").close(); api.notice("已记录人工核对结果");
      }, "draft-confirm-error");
    });
    $("draft-hero-image").addEventListener("error", () => note("draft-error", "商品图片读取失败，请检查本地缓存或数据库同步状态"));
  }
  window.ProductDrafts = { init, refresh, render, hasUnsaved: () => state.dirty };
})();
