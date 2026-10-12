/* PyScope: local, dependency-free application UI (CodeMirror is vendored). */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const TERMINAL = new Set(["completed", "error", "stopped", "limited"]);
  const STATUS = {starting: "正在启动", running: "正在执行", paused: "断点 / 单步暂停", completed: "执行完成", error: "执行异常", stopped: "已停止", limited: "已达运行限制"};
  const EVENT = {line: "即将执行", call: "进入函数", return: "函数返回", exception: "异常事件"};
  const serverMode = document.body.dataset.serverMode === "true";
  let csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  let authRedirecting = false;
  let loggingOut = false;
  const state = {
    examples: [], exampleId: null, filename: "main.py", title: "运行观察台", breakpoints: new Set(),
    session: null, events: [], selected: -1, followLive: true, history: false, frame: -1, scope: "current",
    busy: false, pollTimer: null, pollErrors: 0, replayTimer: null, profile: null, profileCode: "", baseline: null,
    currentHighlight: null, toastTimer: null, dirty: false, currentView: "debug", inspector: "variables"
  };
  const editor = CodeMirror.fromTextArea($("code-editor"), {
    mode: {name: "python", version: 3}, lineNumbers: true, indentUnit: 4, tabSize: 4, indentWithTabs: false,
    lineWrapping: false, gutters: ["breakpoints", "CodeMirror-linenumbers"], viewportMargin: 20,
    extraKeys: {Tab: (cm) => cm.somethingSelected() ? cm.indentSelection("add") : cm.replaceSelection("    ", "end"), "Ctrl-Enter": () => startSession(), F10: () => executeCommand("step"), F8: () => executeCommand("run"), "Shift-F5": () => executeCommand("stop")}
  });
  editor.getInputField().setAttribute("aria-label", "Python 源代码编辑器");
  editor.getWrapperElement().setAttribute("aria-label", "Python 源代码编辑器");
  editor.on("gutterClick", (_cm, line) => toggleBreakpoint(line + 1));
  editor.on("cursorActivity", () => {
    const pos = editor.getCursor();
    $("cursor-position").textContent = `行 ${pos.line + 1}，列 ${pos.ch + 1}`;
  });
  editor.on("change", () => {
    state.dirty = true;
    $("modified-dot").hidden = false;
    for (const line of state.breakpoints) if (line > editor.lineCount()) state.breakpoints.delete(line);
    renderBreakpoints();
    if (state.session && state.session.code !== editor.getValue()) clearHighlight();
    updateControls();
    if (state.profile && state.profileCode !== editor.getValue()) $("profile-run-note").textContent = "代码已修改，报告对应上次运行";
  });

  async function api(path, options = {}) {
    if (authRedirecting) throw new Error("登录状态已失效，正在返回登录页。");
    const method = String(options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    headers.set("Accept", "application/json");
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(path, {...options, method, headers, credentials: "same-origin"});
    if (authRedirecting) throw new Error("登录状态已失效，已忽略先前请求的响应。");
    if (response.status === 401) {
      redirectToLogin();
      throw new Error("登录状态已失效，请重新登录。");
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error(`服务器响应无法读取（HTTP ${response.status}）`); }
    if (authRedirecting) throw new Error("登录状态已失效，已忽略先前请求的响应。");
    if (!response.ok) {
      const error = new Error(data.message || data.error || `请求失败（HTTP ${response.status}）`);
      error.details = data;
      error.status = response.status;
      throw error;
    }
    return data;
  }
  const post = (path, body) => api(path, {method: "POST", body: JSON.stringify(body)});
  const active = () => Boolean(state.session && !state.history && !TERMINAL.has(state.session.status));
  const selectedEvent = () => state.events[state.selected] || null;
  const number = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const ms = (value) => number(value).toLocaleString("zh-CN", {maximumFractionDigits: 2});
  let objectReturnFocus = null;
  let graphResizeFrame = null;

  function redirectToLogin() {
    if (authRedirecting) return;
    authRedirecting = true;
    stopPolling();
    stopReplay();
    clearTimeout(state.toastTimer);
    state.session = null;
    state.events = [];
    state.selected = -1;
    state.profile = state.baseline = null;
    state.profileCode = "";
    state.breakpoints.clear();
    csrfToken = "";
    editor.setValue("");
    editor.clearHistory();
    renderSnapshot();
    $("history-list").replaceChildren();
    $("ast-tree").replaceChildren();
    $("ast-metrics").replaceChildren();
    $("ast-warnings").replaceChildren();
    $("profile-rows").replaceChildren();
    $("profile-output").textContent = "";
    $("session-alert").textContent = "";
    $("toast").hidden = true;
    // A fixed destination avoids copying source or credentials into a URL.
    window.location.replace("/login");
  }

  async function logout() {
    if (!serverMode || loggingOut || authRedirecting) return;
    if (state.busy) { toast("当前操作尚未完成，请稍后退出。"); return; }
    loggingOut = true;
    state.busy = true;
    const button = $("logout-button");
    if (button) button.textContent = "退出中…";
    if (active()) toast("正在停止当前调试并退出登录…");
    stopPolling();
    stopReplay();
    updateControls();
    try {
      // Reuse the existing stop path before revoking the login session.
      // A failed stop or logout leaves the user signed in with an error shown.
      await leaveSession();
      const result = await post("/auth/logout", {});
      if (result?.ok !== true) throw new Error(result?.message || "退出未完成，请重试。");
      redirectToLogin();
    } catch (error) {
      if (!authRedirecting) toast(`退出失败：${error.message}`, true);
    } finally {
      if (!authRedirecting) {
        loggingOut = false;
        state.busy = false;
        if (button) button.textContent = "退出";
        updateControls();
        renderSnapshot();
        schedulePoll();
      }
    }
  }

  // 抽屉是否启用由实际布局决定，避免 JS 与 CSS 使用两套响应式断点。
  function compactWorkspace() {
    const trigger = $("workspace-menu-button");
    return Boolean(trigger && trigger.getClientRects().length && getComputedStyle(trigger).visibility !== "hidden");
  }
  function workspaceOpen() { return document.body.classList.contains("workspace-menu-open"); }
  function setWorkspaceMenu(open, {restoreFocus = true} = {}) {
    const trigger = $("workspace-menu-button"), sidebar = $("workspace-sidebar"), backdrop = $("workspace-backdrop");
    if (!trigger || !sidebar || !backdrop) return;
    const wasOpen = workspaceOpen(), compact = compactWorkspace(), expanded = Boolean(open && compact), hadSidebarFocus = sidebar.contains(document.activeElement);
    document.body.classList.toggle("workspace-menu-open", expanded);
    trigger.setAttribute("aria-expanded", String(expanded));
    backdrop.hidden = !expanded;
    sidebar.inert = compact && !expanded;
    if (compact && !expanded) sidebar.setAttribute("aria-hidden", "true"); else sidebar.removeAttribute("aria-hidden");
    const main = document.querySelector(".main-content");
    if (main) main.inert = expanded;
    if (expanded && !wasOpen) {
      setBreakpointPopover(false, {restoreFocus: false});
      hideObject({restoreFocus: false});
      const first = sidebar.querySelector("button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled)");
      (first || trigger).focus({preventScroll: true});
    } else if (!expanded && (wasOpen || hadSidebarFocus) && restoreFocus && compact) {
      trigger.focus({preventScroll: true});
    }
  }
  function handleWorkspaceTab(event) {
    if (event.key !== "Tab" || !workspaceOpen()) return false;
    const sidebar = $("workspace-sidebar");
    const candidates = [$("workspace-menu-button"), ...sidebar.querySelectorAll("button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex='-1'])"), $("workspace-backdrop")];
    const focusable = [...new Set(candidates)].filter((node) => node && !node.hidden && node.getClientRects().length);
    if (!focusable.length) return false;
    const current = focusable.indexOf(document.activeElement);
    const next = current < 0 ? (event.shiftKey ? focusable.length - 1 : 0) : (current + (event.shiftKey ? -1 : 1) + focusable.length) % focusable.length;
    event.preventDefault();
    focusable[next].focus();
    return true;
  }
  function setBreakpointPopover(open, {restoreFocus = true} = {}) {
    const panel = $("breakpoint-popover"), trigger = $("breakpoint-toggle"), wasOpen = !panel.hidden;
    panel.hidden = !open;
    trigger.setAttribute("aria-controls", "breakpoint-popover");
    trigger.setAttribute("aria-expanded", String(Boolean(open)));
    if (open) $("breakpoint-line").focus();
    else if (wasOpen && restoreFocus) trigger.focus({preventScroll: true});
  }
  function hideObject({restoreFocus = true} = {}) {
    const detail = $("object-detail"), wasOpen = !detail.hidden;
    detail.hidden = true;
    delete detail.dataset.currentObject;
    document.querySelectorAll("[data-object-id]").forEach((node) => node.setAttribute("aria-expanded", "false"));
    if (wasOpen && restoreFocus && objectReturnFocus?.isConnected) objectReturnFocus.focus({preventScroll: true});
    objectReturnFocus = null;
  }
  function findOriginExceptionIndex(events, terminalError) {
    const errorText = String(terminalError || "").trim();
    if (!errorText) return -1;
    let chosen = -1, deepest = -1;
    events.forEach((event, index) => {
      const exceptionText = String(event.exception || "").trim();
      if (event.event !== "exception" || !exceptionText || !errorText.includes(exceptionText)) return;
      const depth = Number.isFinite(Number(event.depth)) ? Number(event.depth) : Math.max(0, (event.stack || []).length - 1);
      // The same exception propagates through outer frames. Prefer its deepest
      // recorded origin, retaining the earliest matching event at that depth.
      if (depth > deepest) { chosen = index; deepest = depth; }
    });
    return chosen;
  }
  function focusException(index) {
    state.selected = index;
    state.followLive = false;
    state.frame = -1;
    state.scope = "current";
    $("scope-select").value = "current";
    setInspector("variables");
  }
  function toast(message, error = false) {
    if (authRedirecting) return;
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").classList.toggle("error", error);
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 7000 : 3500);
  }
  function updateControls() {
    if ($("logout-button")) $("logout-button").disabled = state.busy || loggingOut || authRedirecting;
    const status = state.session?.status;
    const paused = !state.history && status === "paused";
    const running = !state.history && (status === "running" || status === "starting");
    $("start-button").disabled = state.busy || active() || !editor.getValue().trim();
    $("step-button").disabled = state.busy || !paused;
    $("continue-button").disabled = state.busy || !paused;
    $("pause-button").disabled = state.busy || !running;
    $("stop-button").disabled = state.busy || !active();
    $("profile-button").disabled = state.busy || active() || !editor.getValue().trim();
    $("new-file").disabled = state.busy;
    $("import-code").disabled = state.busy;
    document.querySelectorAll(".example-item,.history-item").forEach((button) => { button.disabled = state.busy; });
    editor.setOption("readOnly", active());
    const statusText = state.history ? "已载入历史记录" : (STATUS[status] || "准备就绪");
    // 仅状态改变时更新 live region，避免每次轮询重复播报。
    if ($("status-label").textContent !== statusText) $("status-label").textContent = statusText;
    $("status-dot").className = `status-dot ${status || ""}`;
    const hints = {
      starting: "正在启动独立 Python 进程…", running: "正在采集运行事件，可随时暂停", paused: "高亮行尚未执行 · 可单步或继续",
      completed: "执行完成 · 可拖动时间线回看状态", stopped: "执行已停止 · 已采集的快照仍可回看", error: "执行出现异常 · 查看错误与最后一个快照", limited: "触发运行限制 · 查看下方详情"
    };
    $("execution-hint").textContent = state.history ? "历史快照回放 · 启动调试可重新运行" : (hints[status] || "启动后暂停在第一条可执行语句");
    if (state.session && state.session.code !== editor.getValue()) $("execution-hint").textContent = "代码已修改 · 当前快照来自上一次运行";
    $("session-alert").hidden = !state.session?.error;
    const errorText = state.session?.error || "";
    if ($("session-alert").textContent !== errorText) $("session-alert").textContent = errorText;
    renderTimeline();
  }
  function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; }
  function schedulePoll() {
    stopPolling();
    if (!active() || authRedirecting || loggingOut) return;
    const id = state.session.id;
    state.pollTimer = setTimeout(async () => {
      if (state.session?.id !== id || state.history) return;
      try {
        const last = state.events.length ? state.events[state.events.length - 1].index : -1;
        const data = await api(`/api/sessions/${encodeURIComponent(id)}?after=${last}`);
        if (state.session?.id !== id || state.history) return;
        state.pollErrors = 0;
        applySession(data);
      } catch (error) {
        state.pollErrors++;
        if (state.pollErrors === 3) toast(`无法同步执行状态：${error.message}。正在尝试重新连接。`, true);
      }
      schedulePoll();
    }, state.pollErrors ? 1800 : 320);
  }
  function applySession(data) {
    if (authRedirecting || loggingOut) return;
    const previousStatus = state.session?.status;
    state.session = {...state.session, ...data};
    const oldLength = state.events.length;
    const known = new Map(state.events.map((event) => [event.index, event]));
    for (const event of data.events || []) known.set(event.index, event);
    state.events = Array.from(known.values()).sort((a, b) => a.index - b.index);
    if (state.followLive && state.events.length) {
      state.selected = state.events.length - 1;
      if (oldLength !== state.events.length) state.frame = -1;
    }
    let exceptionLocated = false;
    if (data.status === "error" && previousStatus !== "error" && state.followLive && !state.history) {
      const origin = findOriginExceptionIndex(state.events, data.error);
      if (origin >= 0) { focusException(origin); exceptionLocated = true; }
    }
    updateControls();
    if (data.events?.length || oldLength !== state.events.length || !previousStatus || exceptionLocated) renderSnapshot();
    if (exceptionLocated) toast(`已定位异常起点：第 ${state.events[state.selected].line} 行，可检查当前局部变量`, true);
    if (TERMINAL.has(data.status)) {
      stopPolling();
      if (!state.history && previousStatus !== data.status) loadHistory();
    }
  }
  async function startSession() {
    if (state.busy || active() || !editor.getValue().trim()) return;
    stopReplay();
    setView("debug");
    state.busy = true;
    $("start-button").textContent = "正在启动…";
    updateControls();
    try {
      const data = await post("/api/sessions", {code: editor.getValue(), breakpoints: [...state.breakpoints].sort((a,b) => a-b)});
      state.events = [];
      state.selected = -1;
      state.frame = -1;
      state.history = false;
      state.followLive = true;
      state.session = null;
      state.pollErrors = 0;
      applySession(data);
      schedulePoll();
    } catch (error) { toast(`启动失败：${error.message}`, true); }
    finally { state.busy = false; $("start-button").textContent = "▶ 启动调试"; updateControls(); }
  }
  async function executeCommand(command) {
    if (state.busy || !active()) return;
    if (["step", "run"].includes(command) && state.session.status !== "paused") return;
    if (command === "pause" && !["running", "starting"].includes(state.session.status)) return;
    stopReplay();
    state.followLive = true;
    state.busy = true;
    const id = state.session.id;
    updateControls();
    try {
      const data = await post(`/api/sessions/${encodeURIComponent(id)}/command`, {command, breakpoints: [...state.breakpoints].sort((a,b) => a-b)});
      if (state.session?.id === id) applySession(data);
    } catch (error) { toast(`操作失败：${error.message}`, true); }
    finally { state.busy = false; updateControls(); schedulePoll(); }
  }
  async function leaveSession() {
    stopReplay();
    stopPolling();
    if (active()) {
      try { await post(`/api/sessions/${encodeURIComponent(state.session.id)}/command`, {command: "stop"}); }
      catch (error) { toast(`停止上一运行时遇到问题：${error.message}`, true); throw error; }
    }
    state.session = null; state.events = []; state.selected = -1; state.frame = -1; state.history = false; state.followLive = true;
    clearHighlight();
  }
  async function loadCode(code, title, description, filename = "main.py", exampleId = null) {
    if (state.busy || authRedirecting) return false;
    try { await leaveSession(); } catch (_) { schedulePoll(); return false; }
    if (authRedirecting) return false;
    state.title = title; state.filename = filename; state.exampleId = exampleId; state.breakpoints.clear();
    editor.setValue(code); editor.clearHistory(); editor.setCursor(0, 0);
    state.dirty = false;
    $("modified-dot").hidden = true;
    $("project-title").textContent = title;
    $("project-description").textContent = description || "从一行代码，到完整的运行轨迹。";
    $("file-name").textContent = filename;
    renderBreakpoints(); renderExamples(); updateControls(); renderSnapshot(); setView("debug");
    setWorkspaceMenu(false);
    return true;
  }
  function renderExamples() {
    $("examples-list").replaceChildren();
    $("example-count").textContent = state.examples.length;
    const glyphs = ["ƒ", "↗", "⟳", "{ }", "≠", "⌁"];
    state.examples.forEach((example, index) => {
      const button = make("button", `example-item${example.id === state.exampleId ? " active" : ""}`);
      button.title = example.description;
      button.append(make("span", "example-glyph", glyphs[index % glyphs.length]));
      const labels = make("span");
      labels.append(make("span", "example-name", example.title), make("span", "example-category", example.category || "Python 示例"));
      button.append(labels);
      button.addEventListener("click", () => loadCode(example.code, example.title, example.description, `${example.id}.py`, example.id));
      $("examples-list").append(button);
    });
  }
  async function loadHistory() {
    try {
      const data = await api("/api/history");
      if (authRedirecting) return;
      $("history-list").replaceChildren();
      if (!data.history?.length) { $("history-list").append(make("p", "small muted", serverMode ? "完成一次运行后，记录会自动保存在当前账户。" : "完成一次运行后，记录会自动保存在本地。")); return; }
      for (const item of data.history) {
        const row = make("div", "history-row");
        const button = make("button", "history-item");
        let date = new Date(item.created_at);
        if (typeof item.created_at === "number" && item.created_at < 1e12) date = new Date(item.created_at * 1000);
        const dateText = Number.isNaN(date.getTime()) ? String(item.created_at || "") : date.toLocaleString("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false});
        button.append(make("span", "history-name", item.title || "Python 运行记录"), make("span", "history-meta", `${dateText} · ${item.event_count || 0} 帧 · ${STATUS[item.status] || item.status}`));
        button.addEventListener("click", () => openHistory(item.id));
        const remove = make("button", "icon-button history-delete", "×");
        remove.title = serverMode ? "删除这条账户记录" : "删除这条本地记录"; remove.setAttribute("aria-label", `删除记录：${item.title || item.id}`);
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          try { await api(`/api/history/${encodeURIComponent(item.id)}`, {method: "DELETE"}); if (authRedirecting) return; row.remove(); if (!$("history-list").children.length) loadHistory(); toast(serverMode ? "已删除账户记录" : "已删除本地记录"); }
          catch (error) { toast(error.message, true); remove.disabled = false; }
        });
        row.append(button, remove); $("history-list").append(row);
      }
    } catch (error) { if (!authRedirecting) $("history-list").replaceChildren(make("p", "small muted", `记录暂不可用：${error.message}`)); }
  }
  async function openHistory(id) {
    if (state.busy || authRedirecting) return;
    try {
      const data = await api(`/api/history/${encodeURIComponent(id)}`);
      if (authRedirecting) return;
      await leaveSession();
      if (authRedirecting) return;
      state.history = true; state.followLive = false; state.exampleId = null; state.breakpoints = new Set(data.breakpoints || []);
      state.filename = "history.py";
      editor.setValue(data.code || ""); editor.clearHistory();
      state.dirty = false; $("modified-dot").hidden = true;
      $("file-name").textContent = state.filename;
      $("project-title").textContent = "执行记录回放";
      $("project-description").textContent = serverMode ? "读取当前账户的执行快照，可重新启动调试。" : "读取本地保存的真实执行快照，可重新启动调试。";
      state.selected = (data.events || []).length - 1;
      applySession(data);
      const origin = data.status === "error" ? findOriginExceptionIndex(state.events, data.error) : -1;
      if (origin >= 0) { focusException(origin); renderSnapshot(); }
      renderExamples(); renderBreakpoints(); setView("debug");
      setWorkspaceMenu(false);
      toast(origin >= 0 ? `历史记录已载入，已定位第 ${state.events[origin].line} 行的异常快照` : "历史记录已载入，未重新执行代码");
    } catch (error) { toast(`载入失败：${error.message}`, true); }
  }
  function toggleBreakpoint(line) {
    if (!Number.isInteger(line) || line < 1 || line > editor.lineCount()) { toast("请输入代码范围内的有效行号", true); return; }
    if (state.breakpoints.has(line)) state.breakpoints.delete(line); else state.breakpoints.add(line);
    renderBreakpoints();
    if (active()) {
      // Breakpoint updates are sent with the next command. Running code is not
      // interrupted just because its gutter is clicked.
      toast(state.session.status === "running" ? "断点将在下次调试操作时同步；可先暂停" : "断点将在下一次单步或继续时生效");
    }
  }
  function renderBreakpoints() {
    editor.clearGutter("breakpoints");
    $("breakpoint-list").replaceChildren();
    for (const line of [...state.breakpoints].sort((a,b) => a-b)) {
      const marker = make("div", "breakpoint-marker"); marker.title = `第 ${line} 行断点`;
      editor.setGutterMarker(line - 1, "breakpoints", marker);
      const chip = make("button", "breakpoint-chip", `第 ${line} 行 ×`);
      chip.setAttribute("aria-label", `删除第 ${line} 行断点`);
      chip.addEventListener("click", () => toggleBreakpoint(line));
      $("breakpoint-list").append(chip);
    }
    $("breakpoint-count").textContent = `${state.breakpoints.size} 个断点`;
    $("breakpoint-line").max = editor.lineCount();
    $("clear-breakpoints").disabled = !state.breakpoints.size;
  }
  function clearHighlight() {
    if (state.currentHighlight === null) return;
    for (const name of ["line-current", "line-history"]) editor.removeLineClass(state.currentHighlight, "background", name);
    for (const name of ["line-current-wrap", "line-history-wrap"]) editor.removeLineClass(state.currentHighlight, "wrap", name);
    editor.removeLineClass(state.currentHighlight, "gutter", "line-current-gutter");
    state.currentHighlight = null;
  }
  function renderSnapshot() {
    const event = selectedEvent();
    clearHighlight();
    hideObject({restoreFocus: false});
    if (event?.line > 0 && event.line <= editor.lineCount() && state.session?.code === editor.getValue()) {
      const line = event.line - 1;
      const past = state.history || !state.followLive;
      editor.addLineClass(line, "background", past ? "line-history" : "line-current");
      editor.addLineClass(line, "wrap", past ? "line-history-wrap" : "line-current-wrap");
      editor.addLineClass(line, "gutter", "line-current-gutter");
      state.currentHighlight = line;
      if (state.currentView === "debug") editor.scrollIntoView({line, ch:0}, 60);
    }
    $("event-label").textContent = event ? `${EVENT[event.event] || event.event} · L${event.line || "—"}` : "等待执行";
    $("event-detail").replaceChildren();
    $("event-detail").hidden = !event?.exception && !event?.return_value;
    $("event-detail").classList.toggle("exception", Boolean(event?.exception));
    if (event?.exception) $("event-detail").append(make("span", "", event.exception));
    else if (event?.return_value) $("event-detail").append(make("span", "", "返回值 "), valueNode(event.return_value));
    $("stack-count").textContent = event?.stack?.length || 0;
    const output = event?.output || "";
    $("console-output").textContent = output || (event ? "该快照尚无输出" : "等待程序输出…");
    $("console-output").classList.toggle("is-empty", !output);
    if (state.followLive) $("console-output").scrollTop = $("console-output").scrollHeight;
    renderVariables(event); renderStack(event); renderGraph(event); renderTimeline();
  }
  function currentVariables(event) {
    if (!event) return {};
    if (state.scope === "global") return event.globals || {};
    const stack = event.stack || [];
    const frame = state.frame >= 0 && state.frame < stack.length ? stack[state.frame] : stack[stack.length - 1];
    return frame?.locals || event.globals || {};
  }
  function valueNode(value) {
    if (!value) return make("span", "variable-value", "—");
    if (value.kind === "ref") {
      const button = make("button", "reference-button", `${value.id} ↗ ${value.text || value.type}`);
      button.title = `查看对象 ${value.id}`;
      button.dataset.objectId = value.id;
      button.setAttribute("aria-controls", "object-detail");
      button.setAttribute("aria-expanded", String(!$("object-detail").hidden && $("object-detail").dataset.currentObject === value.id));
      button.setAttribute("aria-label", `查看对象 ${value.id}，${value.type || "引用对象"}，${value.text || ""}`);
      button.addEventListener("click", () => showObject(value.id, button));
      return button;
    }
    const node = make("span", "variable-value", value.text ?? "—"); node.title = value.text ?? ""; return node;
  }
  function renderVariables(event) {
    const target = $("variables-list"); target.replaceChildren();
    const stack = event?.stack || [];
    const frame = state.frame >= 0 && state.frame < stack.length ? stack[state.frame] : stack[stack.length - 1];
    $("scope-select").options[0].textContent = frame ? `栈帧 · ${frame.name}` : "当前栈帧";
    const variables = currentVariables(event);
    const previous = state.selected > 0 ? currentVariables(state.events[state.selected - 1]) : {};
    const entries = Object.entries(variables);
    if (!entries.length) {
      const empty = make("div", "empty-state compact");
      empty.append(make("span", "empty-symbol", "{ }"), make("strong", "", event ? "当前作用域暂无变量" : "变量会在这里浮现"), make("p", "", event ? "执行赋值语句后，查看下一个快照" : "启动调试，观察每一步的状态变化"));
      target.append(empty); return;
    }
    for (const [name, value] of entries) {
      let changed = JSON.stringify(previous[name]) !== JSON.stringify(value);
      if (!changed && value.kind === "ref") {
        const oldObject = state.events[state.selected - 1]?.objects?.find((item) => item.id === value.id);
        const currentObject = event.objects?.find((item) => item.id === value.id);
        changed = JSON.stringify(oldObject) !== JSON.stringify(currentObject);
      }
      const row = make("div", `variable-row${changed ? " changed" : ""}`);
      const nameNode = make("span", "variable-name", name); nameNode.title = name;
      row.append(nameNode, make("span", "variable-type", value.type || ""), valueNode(value)); target.append(row);
    }
  }
  function renderStack(event) {
    const target = $("stack-list"); target.replaceChildren();
    const stack = event?.stack || [];
    if (!stack.length) { target.append(make("div", "empty-state", "启动调试后可查看真实的函数调用栈")); return; }
    stack.forEach((frame, index) => {
      const chosen = state.frame < 0 ? index === stack.length - 1 : index === state.frame;
      const button = make("button", `stack-item${chosen ? " active" : ""}`);
      button.append(make("span", "stack-depth", String(index).padStart(2, "0")), make("span", "stack-name", frame.name), make("span", "stack-line", `第 ${frame.line} 行`));
      button.addEventListener("click", () => { state.frame = index; state.scope = "current"; $("scope-select").value = "current"; setInspector("variables"); renderVariables(event); renderStack(event); document.querySelector('.inspector-tab[data-inspector="variables"]').focus({preventScroll:true}); });
      target.append(button);
    });
  }
  function svgNode(tag, attrs = {}, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function short(text, limit = 22) { const value = String(text ?? ""); return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }
  function graphText(text, maxWidth, size = 13) {
    // 中文按全角估算宽度；完整内容始终保留在对象详情与无障碍名称中。
    const value = String(text ?? ""), limit = Math.max(1, maxWidth / size);
    let used = 0, result = "";
    for (const character of value) {
      const weight = character.codePointAt(0) > 255 ? 1 : 0.62;
      if (used + weight > limit - 1) return `${result}…`;
      result += character; used += weight;
    }
    return result;
  }
  function renderGraph(event) {
    const target = $("object-graph");
    const focusedObject = target.contains(document.activeElement) ? document.activeElement.dataset.objectId : null;
    const returnObject = objectReturnFocus && target.contains(objectReturnFocus) ? objectReturnFocus.dataset.objectId : null;
    const expandedObject = $("object-detail").hidden ? null : $("object-detail").dataset.currentObject;
    target.replaceChildren();
    const all = event?.objects || [];
    $("object-count").textContent = `${all.length} 个对象`;
    if (!all.length) { target.append(make("div", "empty-graph", event ? "当前没有需要展开的引用对象" : "列表、字典和对象之间的真实引用关系")); return; }
    const styles = getComputedStyle(target);
    const availableWidth = target.clientWidth - parseFloat(styles.paddingLeft || 0) - parseFloat(styles.paddingRight || 0);
    const objects = all.slice(0, 24), width = Math.max(220, Math.floor(availableWidth || 360)), columns = width >= 460 ? 2 : 1, gapX = 40, gapY = 42, margin = 14;
    const nodeWidth = Math.floor((width - margin * 2 - gapX * (columns - 1)) / columns), nodeHeight = 142;
    const height = Math.ceil(objects.length / columns) * (nodeHeight + gapY) + 8;
    const svg = svgNode("svg", {width, height, viewBox: `0 0 ${width} ${height}`, role:"group", "aria-label":`当前快照的对象引用关系，显示 ${objects.length} 个对象。可用 Tab 选择节点，按 Enter 或空格查看对象内容。`});
    const defs = svgNode("defs");
    for (const [id,color] of [["arrow-teal","#67bdaa"],["arrow-amber","#d5a65f"]]) {
      const marker = svgNode("marker", {id, viewBox:"0 0 10 10", refX:8, refY:5, markerWidth:5, markerHeight:5, orient:"auto-start-reverse"}); marker.append(svgNode("path", {d:"M 0 0 L 10 5 L 0 10 z", fill:color})); defs.append(marker);
    }
    svg.append(defs);
    const positions = new Map(objects.map((object, index) => [object.id, {x:margin + (index % columns) * (nodeWidth + gapX), y:24 + Math.floor(index / columns) * (nodeHeight + gapY)}]));
    const adjacency = new Map(objects.map((object) => [object.id, (object.items || []).filter((item) => item.value?.kind === "ref").map((item) => item.value.id)]));
    function reaches(from, to, seen = new Set()) {
      if (from === to) return true;
      if (seen.has(from)) return false;
      seen.add(from);
      return (adjacency.get(from) || []).some((next) => reaches(next, to, seen));
    }
    let cycleFound = false;
    const edges = svgNode("g", {fill:"none", "stroke-width":1.5, "aria-hidden":"true"});
    for (const object of objects) {
      const source = positions.get(object.id);
      let edgeOffset = 0;
      for (const item of object.items || []) {
        if (item.value?.kind !== "ref" || !positions.has(item.value.id)) continue;
        const destination = positions.get(item.value.id), self = item.value.id === object.id;
        const cyclic = reaches(item.value.id, object.id);
        cycleFound ||= cyclic;
        let d;
        if (self) {
          const x = source.x + nodeWidth - 18, y = source.y;
          d = `M ${x-22} ${y} C ${x-35} ${y-20}, ${x+30} ${y-20}, ${x+21} ${y+10}`;
        } else if (source.y === destination.y) {
          const leftToRight = source.x < destination.x;
          const sx = source.x + (leftToRight ? nodeWidth : 0), dx = destination.x + (leftToRight ? 0 : nodeWidth);
          const sy = source.y + 65 + (edgeOffset++ % 3)*12, dy = destination.y + 54;
          d = `M ${sx} ${sy} C ${(sx+dx)/2} ${sy}, ${(sx+dx)/2} ${dy}, ${dx} ${dy}`;
        } else {
          const down = source.y < destination.y;
          const sx = source.x + nodeWidth * .4 + (edgeOffset++ % 3)*12, sy = source.y + (down ? nodeHeight : 0), dx = destination.x + nodeWidth * .6, dy = destination.y + (down ? 0 : nodeHeight);
          d = `M ${sx} ${sy} C ${sx} ${(sy+dy)/2}, ${dx} ${(sy+dy)/2}, ${dx} ${dy}`;
        }
        const path = svgNode("path", {class:`graph-edge${cyclic ? " graph-edge-cycle" : ""}`,d,stroke:cyclic?"#edbd70":"#78cfb7",opacity:.85,"marker-end":`url(#arrow-${cyclic?"amber":"teal"})`});
        if (cyclic) path.setAttribute("stroke-dasharray", "3 2");
        path.append(svgNode("title", {}, `${object.id}[${item.label}] → ${item.value.id}${cyclic ? "（循环引用）" : ""}`)); edges.append(path);
      }
    }
    svg.append(edges);
    objects.forEach((object) => {
      const {x,y} = positions.get(object.id);
      const items = object.items || [];
      const references = items.filter((item) => item.value?.kind === "ref").map((item) => `${item.label} 引用 ${item.value.id}`);
      const label = `对象 ${object.id}，类型 ${object.type}，${object.preview || ""}，已记录 ${items.length} 项${references.length ? `；${references.slice(0,3).join("；")}` : ""}。按 Enter 或空格查看详情。`;
      const node = svgNode("g", {class:"graph-node",transform:`translate(${x} ${y})`,tabindex:0,role:"button","data-object-id":object.id,"aria-controls":"object-detail","aria-expanded":String(object.id === expandedObject),"aria-label":label});
      node.append(svgNode("title", {}, label));
      node.append(svgNode("rect", {class:"graph-node-card",width:nodeWidth,height:nodeHeight,rx:8,fill:"#1b292d",stroke:"#527b6d"}));
      node.append(svgNode("text", {class:"graph-node-id",x:13,y:25,fill:"#b4efda","font-size":14,"font-weight":650}, object.id));
      node.append(svgNode("text", {class:"graph-node-type",x:nodeWidth-13,y:25,fill:"#a6c5bb","font-size":13,"text-anchor":"end"}, graphText(object.type,nodeWidth*.6)));
      node.append(svgNode("path", {class:"graph-node-divider",d:`M 1 38 H ${nodeWidth-1}`,stroke:"#3a554c"}));
      items.slice(0,3).forEach((item,index) => {
        const isRef = item.value?.kind === "ref";
        const text = `${short(item.label,12)}  ${isRef ? "→ "+item.value.id : (item.value?.text ?? "—")}`;
        node.append(svgNode("text", {class:`graph-node-item${isRef ? " graph-node-reference" : ""}`,x:13,y:59+index*21,fill:isRef?"#8dd9bf":"#d5e0d9","font-size":13}, graphText(text,nodeWidth-26)));
      });
      if (!items.length) node.append(svgNode("text", {class:"graph-node-item",x:13,y:61,fill:"#a6bdb4","font-size":13}, "空对象"));
      const hint = items.length > 3 ? `另有 ${items.length-3} 项 · 查看详情` : "查看对象详情 ↗";
      node.append(svgNode("text", {class:"graph-node-hint",x:13,y:128,fill:"#a6bdb4","font-size":12.5}, graphText(hint,nodeWidth-26,12.5)));
      node.addEventListener("click", () => showObject(object.id, node));
      node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showObject(object.id, node); } });
      svg.append(node);
    });
    target.append(svg);
    const renderedNodes = [...svg.querySelectorAll(".graph-node")];
    if (focusedObject) renderedNodes.find((node) => node.dataset.objectId === focusedObject)?.focus({preventScroll:true});
    if (returnObject) objectReturnFocus = renderedNodes.find((node) => node.dataset.objectId === returnObject) || null;
    if (cycleFound) target.append(make("p", "graph-cycle-note", "琥珀色虚线表示参与循环的引用关系"));
    if (all.length > objects.length) target.append(make("p", "small muted", `图中展示前 ${objects.length} 个对象；其他对象可通过变量引用查看。`));
  }
  function showObject(id, source = document.activeElement) {
    const object = selectedEvent()?.objects?.find((item) => item.id === id);
    if (!object) { toast("该对象未被展开记录，可能达到快照深度或数量限制"); return; }
    const target = $("object-detail");
    if (!target.contains(source)) objectReturnFocus = source;
    target.replaceChildren();
    target.dataset.currentObject = id;
    const head = make("div", "object-detail-head"); head.append(make("strong", "", `${object.id} · ${object.type}`));
    const close = make("button", "icon-button", "×"); close.setAttribute("aria-label", "关闭对象详情"); close.addEventListener("click", () => hideObject()); head.append(close); target.append(head);
    target.append(make("p", "object-detail-preview", object.preview || ""));
    for (const item of object.items || []) {
      const row = make("div", "object-item-row"); row.append(make("span", "", item.label), valueNode(item.value)); target.append(row);
    }
    if (!object.items?.length) target.append(make("p", "muted small", "空对象或无可展开属性。"));
    target.setAttribute("role", "region"); target.setAttribute("aria-label", `对象 ${object.id} 的详细内容`);
    document.querySelectorAll("[data-object-id]").forEach((node) => node.setAttribute("aria-expanded", String(node.dataset.objectId === id)));
    target.hidden = false; close.focus({preventScroll:true});
    target.scrollIntoView({block:"nearest",behavior:"auto"});
  }
  function renderTimeline() {
    const count = state.events.length, event = selectedEvent();
    $("timeline").max = Math.max(0,count-1); $("timeline").value = Math.max(0,state.selected); $("timeline").disabled = !count;
    $("timeline-start").textContent = count ? "1" : "0";
    $("timeline-end").textContent = count;
    $("snapshot-label").textContent = count ? `快照 ${state.selected+1} / ${count}` : "尚无快照";
    $("elapsed-label").textContent = `${ms(event?.elapsed_ms)} ms`;
    $("replay-badge").hidden = !count || (!state.history && state.followLive);
    $("previous-event").disabled = state.selected <= 0;
    $("next-event").disabled = state.selected >= count-1;
    $("play-replay").disabled = count < 2;
    $("go-live").disabled = !count || (state.followLive && !state.history);
    $("go-live").textContent = state.history ? "末尾快照" : "最新状态";
    $("export-record").disabled = !count;
  }
  function selectSnapshot(index, follow = false) {
    if (!state.events.length) return;
    state.selected = Math.max(0, Math.min(index,state.events.length-1)); state.followLive = follow; state.frame = -1; renderSnapshot();
  }
  function stopReplay() { clearInterval(state.replayTimer); state.replayTimer = null; $("play-replay").textContent = "▷"; $("play-replay").setAttribute("aria-label", "播放快照"); }
  function playReplay() {
    if (state.replayTimer) { stopReplay(); return; }
    if (state.events.length < 2) return;
    if (state.selected >= state.events.length-1) selectSnapshot(0);
    state.followLive = false; $("play-replay").textContent = "Ⅱ"; $("play-replay").setAttribute("aria-label", "暂停回放");
    state.replayTimer = setInterval(() => { if (state.selected >= state.events.length-1) { stopReplay(); return; } selectSnapshot(state.selected+1); }, 450);
  }
  function setView(view) {
    state.currentView = view;
    document.querySelectorAll(".view-tab").forEach((button) => { const selected = button.dataset.view === view; button.classList.toggle("active",selected); button.setAttribute("aria-selected",String(selected)); button.tabIndex = selected ? 0 : -1; });
    for (const name of ["debug","ast","profile"]) $(`view-${name}`).hidden = name !== view;
    if (view !== "debug") { setBreakpointPopover(false, {restoreFocus:false}); hideObject({restoreFocus:false}); }
    if (view === "debug") requestAnimationFrame(() => editor.refresh());
  }
  function setInspector(name) {
    state.inspector = name;
    document.querySelectorAll(".inspector-tab").forEach((button) => { const selected = button.dataset.inspector === name; button.classList.toggle("active",selected); button.setAttribute("aria-selected",String(selected)); button.tabIndex = selected ? 0 : -1; });
    $("inspector-variables").hidden = name !== "variables"; $("inspector-stack").hidden = name !== "stack";
  }
  function bindTabs(selector, dataKey, activate) {
    const buttons = [...document.querySelectorAll(selector)];
    const container = buttons[0]?.parentElement;
    if (container) { container.setAttribute("role", "tablist"); container.setAttribute("aria-orientation", "horizontal"); }
    buttons.forEach((button, index) => {
      button.setAttribute("role", "tab");
      button.tabIndex = button.classList.contains("active") ? 0 : -1;
      button.setAttribute("aria-selected", String(button.classList.contains("active")));
      button.addEventListener("click", () => activate(button.dataset[dataKey]));
      button.addEventListener("keydown", (event) => {
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
        else if (event.key === "ArrowLeft") next = (index - 1 + buttons.length) % buttons.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = buttons.length - 1;
        else return;
        event.preventDefault();
        activate(buttons[next].dataset[dataKey]);
        buttons[next].focus({preventScroll:true});
      });
    });
  }
  async function analyzeCode() {
    if (authRedirecting) return;
    const button = $("analyze-button"); button.disabled = true; button.textContent = "正在解析…";
    try {
      const data = await post("/api/analyze", {code:editor.getValue()});
      if (authRedirecting) return;
      $("ast-tree").replaceChildren(); $("ast-metrics").replaceChildren();
      for (const [key,label] of [["lines","代码行数"],["functions","函数定义"],["classes","类定义"],["branches","分支节点"],["loops","循环节点"]]) {
        const card = make("div","metric-card"); card.append(make("span","",label),make("strong","",data.counts?.[key] ?? 0)); $("ast-metrics").append(card);
      }
      let remaining = 1800;
      function build(node,depth=0) {
        if (--remaining < 0 || depth > 50) return make("div","ast-leaf","… 结构过深或节点过多，已折叠显示");
        const children = node.children || [];
        const row = make(children.length ? "details" : "div",children.length ? "" : "ast-leaf");
        const label = children.length ? make("summary") : row;
        label.append(document.createTextNode(node.label || "节点"));
        if (node.line) {
          const line = make("button","ast-line",`L${node.line}`); line.title = "跳转到源代码";
          line.addEventListener("click",(e) => { e.preventDefault(); e.stopPropagation(); setView("debug"); editor.setCursor(Math.max(0,node.line-1),0); editor.focus(); }); label.append(line);
        }
        if (children.length) { row.open = depth < 2; row.append(label); for (const child of children) { if (remaining < 0) break; row.append(build(child,depth+1)); } }
        return row;
      }
      if (data.tree) $("ast-tree").append(build(data.tree));
      $("ast-summary").textContent = `已解析 · ${new Date().toLocaleTimeString("zh-CN",{hour12:false})}`;
      $("ast-warnings").replaceChildren();
      for (const warning of data.warnings || []) $("ast-warnings").append(make("div","warning-row",typeof warning === "string" ? warning : JSON.stringify(warning)));
      $("ast-warnings").hidden = !data.warnings?.length;
    } catch (error) {
      if (authRedirecting) return;
      $("ast-tree").replaceChildren(make("div","ast-error",`解析失败${error.details?.line ? ` · 第 ${error.details.line} 行` : ""}\n${error.message}`));
      $("ast-summary").textContent = "代码存在语法错误或暂不可解析"; $("ast-metrics").replaceChildren(); $("ast-warnings").hidden = true;
    } finally { if (!authRedirecting) { button.disabled = false; button.textContent = "↻ 解析当前代码"; } }
  }
  async function runProfile() {
    if (state.busy || active() || authRedirecting) return;
    state.busy = true; updateControls(); $("profile-button").textContent = "正在采集性能…";
    const code = editor.getValue();
    try {
      const result = await post("/api/profile",{code});
      if (authRedirecting) return;
      state.profile = result; state.profileCode = code;
      renderProfile();
      if (result.exception) toast("性能运行发生异常，已保留运行输出与已采集的数据",true);
      else toast("真实性能报告已生成");
    } catch (error) { toast(`性能分析失败：${error.message}`,true); }
    finally { state.busy = false; $("profile-button").textContent = "▶ 运行性能分析"; updateControls(); }
  }
  function metricValue(id,value) { $(id).replaceChildren(document.createTextNode(ms(value)),make("small","","ms")); }
  function renderProfile() {
    const result = state.profile;
    if (!result) return;
    metricValue("profile-elapsed",result.elapsed_ms);
    $("profile-run-note").textContent = result.exception ? "运行发生异常，以下为已采集的数据" : (state.profileCode === editor.getValue() ? "独立执行 · 不含调试快照采集" : "代码已修改，报告对应上次运行");
    $("save-baseline").disabled = Boolean(result.exception);
    $("profile-output").textContent = `${result.output || "（本次运行没有标准输出）"}${result.exception ? `\n\n[执行异常]\n${result.exception}` : ""}`;
    $("profile-rows").replaceChildren();
    const functions = [...(result.functions || [])].sort((a,b)=>number(b.cumulative_ms)-number(a.cumulative_ms));
    const maximum = Math.max(0.001,...functions.map((row)=>number(row.cumulative_ms)));
    if (!functions.length) { const row=make("tr"),cell=make("td","table-empty","此次运行没有可显示的函数采样数据");cell.colSpan=5;row.append(cell);$("profile-rows").append(row); }
    for (const item of functions) {
      const row=make("tr"),name=make("td","",`${item.name}${item.line ? ` · L${item.line}` : ""}`);name.title=item.name;
      row.append(name,make("td","",item.calls),make("td","",`${ms(item.total_ms)} ms`),make("td","",`${ms(item.cumulative_ms)} ms`));
      const cell=make("td"),bar=make("div","profile-bar"),fill=make("span");fill.style.width=`${Math.max(1,number(item.cumulative_ms)/maximum*100)}%`;bar.title="相对本次最长累计耗时函数的比例";bar.append(fill);cell.append(bar);row.append(cell);$("profile-rows").append(row);
    }
    if (state.baseline) {
      metricValue("baseline-elapsed",state.baseline.result.elapsed_ms);
      const base = number(state.baseline.result.elapsed_ms),current=number(result.elapsed_ms),delta=base ? ((current-base)/base)*100 : 0;
      $("profile-difference").textContent = base ? `${delta > 0 ? "+" : ""}${delta.toFixed(1)}%` : "—";
      $("profile-difference").className = delta < 0 ? "improved" : "regressed";
      $("comparison-note").textContent = base ? `${delta < 0 ? "本次更快" : delta > 0 ? "本次更慢" : "耗时相同"} · ${state.baseline.code === state.profileCode ? "相同代码" : "代码已变更"}` : "基准耗时太小，无法计算有效比例";
    }
  }
  function download(blob,filename) {
    const url=URL.createObjectURL(blob),link=make("a");link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function exportRecord() {
    if (!state.events.length || !state.session || authRedirecting) return;
    try {
      let blob;
      if (state.history) blob=new Blob([JSON.stringify({format:"pyscope-trace-v1",...state.session,events:state.events,total_events:state.events.length},null,2)],{type:"application/json;charset=utf-8"});
      else {
        const response=await fetch(`/api/sessions/${encodeURIComponent(state.session.id)}/export`, {credentials:"same-origin"});
        if (authRedirecting) return;
        if (response.status === 401) { redirectToLogin(); return; }
        if (!response.ok) throw new Error(`导出失败（HTTP ${response.status}）`);
        blob=await response.blob();
      }
      if (authRedirecting) return;
      download(blob,`pyscope-trace-${String(state.session.id).slice(0,8)}.json`);toast("执行记录已导出");
    } catch(error) { toast(error.message,true); }
  }

  $("start-button").addEventListener("click",startSession);
  $("logout-button")?.addEventListener("click",logout);
  $("step-button").addEventListener("click",()=>executeCommand("step"));
  $("continue-button").addEventListener("click",()=>executeCommand("run"));
  $("pause-button").addEventListener("click",()=>executeCommand("pause"));
  $("stop-button").addEventListener("click",()=>executeCommand("stop"));
  $("new-file").addEventListener("click",()=>loadCode("# 在这里编写你的 Python 程序\n\nmessage = \"Hello, PyScope!\"\nprint(message)\n","未命名程序","从一行代码，到完整的运行轨迹。"));
  $("refresh-history").addEventListener("click",loadHistory);
  $("breakpoint-toggle").addEventListener("click",()=>setBreakpointPopover($("breakpoint-popover").hidden));
  $("close-breakpoints").addEventListener("click",()=>setBreakpointPopover(false));
  $("add-breakpoint").addEventListener("click",()=>toggleBreakpoint(Number($("breakpoint-line").value)));
  $("breakpoint-line").addEventListener("keydown",(e)=>{if(e.key==="Enter"){e.preventDefault();toggleBreakpoint(Number(e.target.value));}});
  $("clear-breakpoints").addEventListener("click",()=>{state.breakpoints.clear();renderBreakpoints();});
  $("scope-select").addEventListener("change",(e)=>{state.scope=e.target.value;renderVariables(selectedEvent());});
  $("timeline").addEventListener("input",(e)=>{stopReplay();selectSnapshot(Number(e.target.value));});
  $("previous-event").addEventListener("click",()=>{stopReplay();selectSnapshot(state.selected-1);});
  $("next-event").addEventListener("click",()=>{stopReplay();selectSnapshot(state.selected+1);});
  $("play-replay").addEventListener("click",playReplay);
  $("go-live").addEventListener("click",()=>{stopReplay();selectSnapshot(state.events.length-1,!state.history);});
  $("export-record").addEventListener("click",exportRecord);
  $("analyze-button").addEventListener("click",analyzeCode);
  $("profile-button").addEventListener("click",runProfile);
  $("save-baseline").addEventListener("click",()=>{if(!state.profile||state.profile.exception)return;state.baseline={result:structuredClone(state.profile),code:state.profileCode};renderProfile();toast("已保存本次结果作为性能对比基准");});
  bindTabs(".view-tab", "view", setView);
  bindTabs(".inspector-tab", "inspector", setInspector);
  $("workspace-menu-button")?.addEventListener("click",()=>setWorkspaceMenu(!workspaceOpen()));
  $("workspace-backdrop")?.addEventListener("click",()=>setWorkspaceMenu(false));
  $("import-code").addEventListener("click",()=>$("code-file-input").click());
  $("code-file-input").addEventListener("change",async(e)=>{
    const file=e.target.files[0];e.target.value="";if(!file)return;
    if(file.size>1024*1024){toast("代码文件超过 1 MB，请选择较小的 .py 文件",true);return;}
    try {const loaded=await loadCode(await file.text(),file.name.replace(/\.py$/i,""),"已导入本地 Python 文件，可设置断点并开始调试。",file.name);if(loaded)toast("代码已导入");}
    catch(error){toast(`导入失败：${error.message}`,true);}
  });
  $("export-code").addEventListener("click",()=>{download(new Blob([editor.getValue()],{type:"text/x-python;charset=utf-8"}),state.filename.endsWith(".py")?state.filename:`${state.filename}.py`);state.dirty=false;$("modified-dot").hidden=true;});
  $("help-button").addEventListener("click",()=>{setWorkspaceMenu(false,{restoreFocus:false});$("help-dialog").showModal();});
  $("close-help").addEventListener("click",()=>$("help-dialog").close());
  $("help-dialog").addEventListener("click",(event)=>{if(event.target===$("help-dialog")){const rect=$("help-dialog").getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$("help-dialog").close();}});
  document.addEventListener("keydown",(event)=>{
    if(event.key==="Escape"){
      stopReplay();
      if($("help-dialog").open)return;
      if(workspaceOpen()){event.preventDefault();setWorkspaceMenu(false);return;}
      if(!$("breakpoint-popover").hidden){event.preventDefault();setBreakpointPopover(false);return;}
      if(!$("object-detail").hidden){event.preventDefault();hideObject();return;}
    }
    if(handleWorkspaceTab(event))return;
    if(event.defaultPrevented||workspaceOpen()||event.target.closest(".CodeMirror"))return;
    if((event.ctrlKey||event.metaKey)&&event.key==="Enter"){event.preventDefault();startSession();}
    else if(event.key==="F10"){event.preventDefault();executeCommand("step");}
    else if(event.key==="F8"){event.preventDefault();executeCommand("run");}
    else if(event.shiftKey&&event.key==="F5"){event.preventDefault();executeCommand("stop");}
  });
  window.addEventListener("resize",()=>{
    setWorkspaceMenu(workspaceOpen());
    editor.refresh();
    if(!window.ResizeObserver){cancelAnimationFrame(graphResizeFrame);graphResizeFrame=requestAnimationFrame(()=>renderGraph(selectedEvent()));}
  });
  if(window.ResizeObserver){
    let observedWidth = -1;
    const graphObserver = new ResizeObserver(()=>{
      const width = $("object-graph").clientWidth;
      if(width === observedWidth)return;
      observedWidth = width;
      if(width>0){cancelAnimationFrame(graphResizeFrame);graphResizeFrame=requestAnimationFrame(()=>renderGraph(selectedEvent()));}
    });
    graphObserver.observe($("object-graph"));
  }
  setBreakpointPopover(false,{restoreFocus:false});
  setWorkspaceMenu(false,{restoreFocus:false});
  window.addEventListener("beforeunload",(event)=>{if(active()){event.preventDefault();event.returnValue="";}});

  async function initialize() {
    if (serverMode) {
      try {
        const account = await api("/api/auth/me");
        if (authRedirecting) return;
        if (account.authenticated !== true) { redirectToLogin(); return; }
        if (typeof account.csrf_token === "string" && account.csrf_token) csrfToken = account.csrf_token;
        if ($("current-account") && typeof account.username === "string") {
          $("current-account").textContent = account.username;
          $("current-account").title = `当前账户：${account.username}`;
        }
      } catch (error) {
        if (!authRedirecting) {
          state.busy = true;
          updateControls();
          $("examples-list").replaceChildren(make("p", "small muted", "无法验证登录状态，请刷新页面重试。"));
          toast(`无法验证登录状态：${error.message}`, true);
        }
        return;
      }
    }
    loadHistory();
    try {
      const data=await api("/api/examples");if(authRedirecting)return;state.examples=data.examples||[];
      if(state.examples.length){const first=state.examples[0];await loadCode(first.code,first.title,first.description,`${first.id}.py`,first.id);}
      else {renderExamples();await loadCode("message = \"Hello, PyScope!\"\nprint(message)\n","运行观察台","编写代码并开始一次可视化调试。");}
    } catch(error) {
      if (authRedirecting) return;
      $("examples-list").replaceChildren(make("p","small muted",`示例载入失败：${error.message}`));
      editor.setValue("message = \"Hello, PyScope!\"\nprint(message)\n");toast("示例暂不可用，仍可编写自己的代码",true);
    }
    updateControls();editor.refresh();
  }
  initialize();
})();
