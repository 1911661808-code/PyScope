"use strict";

(() => {
  const form = document.getElementById("login-form");
  if (!form) return;
  const username = document.getElementById("login-username");
  const password = document.getElementById("login-password");
  const toggle = document.getElementById("toggle-password");
  const submit = document.getElementById("login-submit");
  const label = document.getElementById("login-submit-label");
  const status = document.getElementById("login-status");
  const errorBox = document.getElementById("login-error");
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  let busy = false;

  function setBusy(value) {
    busy = value;
    form.setAttribute("aria-busy", String(value));
    username.disabled = password.disabled = toggle.disabled = submit.disabled = value;
    label.textContent = value ? "正在验证…" : "登录工作空间";
  }
  function showError(message, field) {
    errorBox.textContent = message;
    errorBox.hidden = false;
    status.textContent = "";
    if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
    else errorBox.focus();
  }
  for (const field of [username, password]) field.addEventListener("input", () => {
    field.removeAttribute("aria-invalid");
    if (!busy) errorBox.hidden = true;
  });
  toggle.addEventListener("click", () => {
    const visible = password.type === "password";
    password.type = visible ? "text" : "password";
    toggle.textContent = visible ? "隐藏" : "显示";
    toggle.setAttribute("aria-pressed", String(visible));
    toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    errorBox.hidden = true;
    username.removeAttribute("aria-invalid");
    password.removeAttribute("aria-invalid");
    if (!username.value.trim()) { showError("请输入账户。", username); return; }
    if (!password.value) { showError("请输入密码。", password); return; }
    if (!csrfToken) { showError("登录页面已失效，请刷新页面后重试。"); return; }
    const credentials = {username: username.value.trim(), password: password.value};
    setBusy(true);
    status.textContent = "正在验证账户，请稍候。";
    let authenticated = false;
    try {
      const response = await fetch("/auth/login", {
        method: "POST", credentials: "same-origin", redirect: "error",
        headers: {"Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrfToken},
        body: JSON.stringify(credentials)
      });
      let result = null;
      try { result = await response.json(); } catch (_) { /* Show a local error, never an HTML response. */ }
      if (!response.ok || result?.ok !== true) {
        const fallback = response.status === 429 ? "登录尝试次数过多，请稍后再试。" : response.status === 403 ? "登录页面已失效，请刷新页面后重试。" : response.status >= 500 ? "登录服务暂时不可用，请稍后重试。" : "账户或密码不正确，请重试。";
        const message = response.status < 500 && typeof result?.message === "string" && result.message.trim() ? result.message.slice(0, 300) : fallback;
        setBusy(false);
        showError(message);
        return;
      }
      authenticated = true;
      password.value = "";
      label.textContent = "登录成功";
      status.textContent = "登录成功，正在打开工作空间。";
      // The only supported post-login destination is the workspace itself.
      // No user input, password, source code, or arbitrary next URL is persisted.
      window.location.replace("/");
    } catch (_) {
      setBusy(false);
      showError("无法连接登录服务，请检查网络后重试。");
    } finally {
      credentials.password = "";
      if (!authenticated) setBusy(false);
    }
  });
})();
