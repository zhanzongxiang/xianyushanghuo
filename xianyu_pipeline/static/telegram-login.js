/* Telegram account authorization stays inside the local workbench. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let api, timer, revision = 0, pending = false, completed = false;
  let status = { state: "idle", active: false }, actionError = "", statusError = "", imageError = "";
  const labels = { idle: "尚未登录", initializing: "正在连接 Telegram", qr_pending: "正在获取二维码",
    qr: "请使用已登录的手机 Telegram 扫码确认", phone: "请输入绑定 Telegram 的手机号",
    code: "请输入 Telegram 发来的验证码", password: "请输入 Telegram 两步验证密码",
    finishing: "正在保存登录会话", closing: "正在关闭登录连接", ready: "Telegram 已登录",
    failed: "登录失败", cancelled: "登录已取消", expired: "登录等待超时" };

  function render() {
    if (!$("telegram-auth-dialog").open) return;
    $("telegram-auth-status").textContent = labels[status.state] || "正在读取登录状态";
    const error = actionError || status.error || imageError || statusError;
    $("telegram-auth-error").textContent = error;
    $("telegram-auth-error").hidden = !error;
    const waiting = pending || status.auth_pending || ["loading", "initializing", "qr_pending", "closing", "finishing"].includes(status.state);
    for (const id of ["telegram-auth-qr", "telegram-auth-phone", "telegram-auth-retry"]) $(id).disabled = waiting;
    $("telegram-auth-methods").hidden = ["ready", "finishing", "closing"].includes(status.state);
    $("telegram-auth-retry").hidden = !error && !["failed", "expired", "cancelled"].includes(status.state);
    const fields = { phone: ["手机号（含国际区号）", "tel", "+8613800000000"],
      code: ["登录验证码", "text", ""], password: ["两步验证密码", "password", ""] };
    const field = fields[status.state], input = $("telegram-auth-value");
    $("telegram-auth-field").hidden = !field;
    $("telegram-auth-submit").hidden = !field;
    $("telegram-auth-submit").disabled = waiting;
    input.required = Boolean(field);
    input.disabled = waiting;
    if (field) {
      $("telegram-auth-label").textContent = field[0];
      input.type = field[1];
      input.placeholder = field[2];
      input.inputMode = status.state === "code" ? "numeric" : status.state === "phone" ? "tel" : "text";
    }
    const image = $("telegram-auth-image"), show = status.state === "qr" && status.qr_url;
    image.hidden = !show;
    if (show && image.getAttribute("src") !== status.qr_url) { imageError = ""; image.src = status.qr_url; }
    if (!show) image.removeAttribute("src");
    $("telegram-auth-expiry").hidden = !show;
    $("telegram-auth-expiry").textContent = status.expires_in > 0 ? `${status.expires_in} 秒后自动刷新` : "二维码正在刷新";
  }

  function update(next) {
    const changed = status.state !== next.state;
    status = next;
    if (changed) $("telegram-auth-value").value = "";
    render();
    if (changed && ["phone", "code", "password"].includes(status.state)) $("telegram-auth-value").focus();
    if (status.state === "ready" && !status.active && $("telegram-auth-dialog").open) {
      completed = true;
      $("telegram-auth-dialog").close();
      api.notice("Telegram 登录成功，会话已保存在本机");
      void api.refresh();
    }
  }

  async function poll(epoch) {
    if (epoch !== revision || !$("telegram-auth-dialog").open) return;
    try {
      const next = await api.request("/api/telegram-auth/status");
      if (epoch !== revision) return;
      statusError = "";
      update(next);
    } catch (error) {
      if (epoch !== revision) return;
      statusError = error.message;
      render();
    }
    clearTimeout(timer);
    if (epoch === revision && $("telegram-auth-dialog").open) timer = setTimeout(() => void poll(epoch), 1000);
  }

  async function begin(method) {
    if (pending) return;
    const epoch = ++revision;
    clearTimeout(timer);
    pending = true;
    actionError = statusError = imageError = "";
    $("telegram-auth-value").value = "";
    render();
    try {
      if (status.active && status.ticket) {
        let next = await api.post("/api/telegram-auth/cancel", { ticket: status.ticket });
        // The old client must release its SQLite session before the next connects.
        for (let count = 0; next.active && count < 40; count++) {
          if (epoch !== revision) return;
          update(next);
          await new Promise((resolve) => setTimeout(resolve, 200));
          next = await api.request("/api/telegram-auth/status");
        }
        if (next.active) throw new Error("旧登录连接尚未关闭，请稍后重试");
      }
      if (epoch !== revision) return;
      const next = await api.post("/api/telegram-api-login", { method });
      if (epoch !== revision) {
        if (next.active && next.ticket) await api.post("/api/telegram-auth/cancel", { ticket: next.ticket });
        return;
      }
      update(next);
    } catch (error) {
      if (epoch !== revision) return;
      actionError = error.message;
    } finally {
      pending = false;
      if (epoch === revision) { render(); void poll(epoch); }
    }
  }

  async function open() {
    if ($("telegram-auth-dialog").open) return;
    const epoch = ++revision;
    completed = false;
    actionError = statusError = imageError = "";
    status = { state: "loading", active: false };
    $("telegram-auth-dialog").showModal();
    render();
    try {
      const next = await api.request("/api/telegram-auth/status");
      if (epoch !== revision) return;
      if (next.active) { update(next); void poll(epoch); }
      else { status = next; await begin("qr"); }
    } catch (error) {
      if (epoch !== revision) return;
      actionError = error.message;
      status.state = "failed";
      render();
      void poll(epoch);
    }
  }

  function init(bridge) {
    api = bridge;
    $("telegram-auth-qr").addEventListener("click", () => void begin("qr"));
    $("telegram-auth-phone").addEventListener("click", () => void begin("phone"));
    $("telegram-auth-retry").addEventListener("click", () => void begin(status.method || "qr"));
    $("telegram-auth-close").addEventListener("click", () => $("telegram-auth-dialog").close());
    $("telegram-auth-dialog").addEventListener("close", () => {
      revision++;
      clearTimeout(timer);
      $("telegram-auth-value").value = "";
      $("telegram-auth-image").removeAttribute("src");
      if (!completed && status.active && status.ticket) {
        void api.post("/api/telegram-auth/cancel", { ticket: status.ticket }).then(api.refresh).catch((error) => api.notice(error.message));
      }
    });
    $("telegram-auth-image").addEventListener("error", () => {
      if (status.state === "qr") {
        imageError = "二维码图片读取失败，请等待刷新或点击重新尝试";
        render();
      }
    });
    $("telegram-auth-image").addEventListener("load", () => { imageError = ""; render(); });
    $("telegram-auth-form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (pending || status.auth_pending || !$("telegram-auth-value").value) return;
      const epoch = ++revision, field = status.state, value = $("telegram-auth-value").value;
      clearTimeout(timer);
      $("telegram-auth-value").value = "";
      pending = true;
      actionError = "";
      render();
      void (async () => {
        try {
          const next = await api.post("/api/telegram-auth/auth", { ticket: status.ticket, field, value });
          if (epoch === revision) update(next);
        } catch (error) {
          if (epoch === revision) actionError = error.message;
        } finally {
          pending = false;
          if (epoch === revision) { render(); void poll(epoch); }
        }
      })();
    });
  }
  window.TelegramLogin = { init, open };
})();
