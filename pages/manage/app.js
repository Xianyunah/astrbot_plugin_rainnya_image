/* ============================================================
 * 辉宝生图 · NewImage 管理台 · app.js
 * 纯原生 JS，零依赖。经 window.AstrBotPluginPage bridge 调用后端。
 * 4 个 Tab：模型管理 / 次数管理 / 生图日志 / 基础设置。
 * ============================================================ */
(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage;
  var ctx = null;

  /* ---------- 工具 ---------- */
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  /* 时间戳：后端格式 %Y%m%d_%H%M%S_%f，如 20260830_023945_123456 */
  function fmtLogTime(ts) {
    if (!ts) return "未知时间";
    var m = String(ts).match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
    if (!m) return String(ts);
    return m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] + ":" + m[6];
  }
  function logTsOf(ts) {
    var m = String(ts || "").match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
    if (!m) return 0;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
  }
  function shortTs(ts) {
    var m = String(ts || "").match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/);
    return m ? m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] : (ts || "");
  }

  /* ---------- Toast ---------- */
  function toast(msg, type) {
    var wrap = el("toastWrap");
    var t = document.createElement("div");
    t.className = "toast " + (type || "info");
    t.innerHTML = '<span class="dot"></span><span>' + esc(msg) + "</span>";
    wrap.appendChild(t);
    setTimeout(function () { t.remove(); }, 3200);
  }

  /* ---------- Modal 通用 ---------- */
  function openModal(id) { el(id).hidden = false; }
  function closeModal(id) { el(id).hidden = true; }

  /* ---------- 通用确认弹层（替代原生 confirm，iframe 内禁用原生） ---------- */
  var confirmResolve = null; // 当前确认弹层的 resolve 回调
  function confirmDialog(msg, title) {
    return new Promise(function (resolve) {
      confirmResolve = resolve;
      el("confirmTitle").textContent = title || "确认操作";
      el("confirmMsg").textContent = msg || "";
      openModal("confirmModal");
    });
  }
  function confirmFinish(result) {
    if (confirmResolve) {
      confirmResolve(result);
      confirmResolve = null;
    }
    closeModal("confirmModal");
  }

  /* ---------- bridge 封装 ---------- */
  async function apiGet(endpoint, params) { return await bridge.apiGet(endpoint, params || {}); }
  async function apiPost(endpoint, body) { return await bridge.apiPost(endpoint, body || {}); }

  /* ============================================================
   * Tab 1 · 提供商管理
   * ============================================================ */
  var providersData = [];    // [{id, name, type, api_key, api_key_masked, base_url, enabled, model_count}]
  var providerView = "list"; // "list" | "detail"
  var currentProviderId = null;
  var modelsData = []; // [{name, quota, desc, params:{}, allowed_sizes:null|[]}]

  var TYPE_META = {
    gemini:  { label: "Gemini",  cls: "type-gemini"  },
    openai:  { label: "OpenAI",  cls: "type-openai"  },
    novalai: { label: "NovalAI", cls: "type-novalai" },
  };
  /* 模板默认 BaseURL（用户可改，选择模板时若字段为空则预填） */
  var PROVIDER_TEMPLATES = {
    gemini:  { base_url: "https://generativelanguage.googleapis.com" },
    openai:  { base_url: "https://api.openai.com/v1" },
    novalai: { base_url: "https://image.novelai.net" },
  };
  function typeMeta(t) { return TYPE_META[t] || { label: String(t || "?").toUpperCase(), cls: "" }; }
  function maskKey(k) {
    var s = String(k || "");
    if (!s) return "未设置";
    if (s.length <= 8) return s.slice(0, 2) + "****";
    return s.slice(0, 4) + "…" + s.slice(-4);
  }
  var SIZE_OPTIONS = [
    { label: "不限制", value: "" },
    { label: "仅 1K", value: "1K" },
    { label: "1K / 2K", value: "1K,2K" },
    { label: "1K / 4K", value: "1K,4K" },
    { label: "仅 2K", value: "2K" },
    { label: "2K / 4K", value: "2K,4K" },
    { label: "仅 4K", value: "4K" },
    { label: "1K / 2K / 4K", value: "1K,2K,4K" },
  ];
  function sizesToSel(v) {
    if (!Array.isArray(v) || !v.length) return "";
    return SIZE_OPTIONS.filter(function (o) { return o.value && v.indexOf(o.value) !== -1; })
      .map(function (o) { return o.value; }).join(",");
  }
  function sizesFromSel(v) {
    var arr = String(v || "").split(",").filter(function (s) { return s; });
    return arr.length ? arr : null;
  }

  function renderModels() {
    var body = el("modelsBody");
    if (!modelsData.length) {
      body.innerHTML =
        '<tr class="row-empty"><td colspan="6"><div class="empty">' +
        '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>' +
        '<span class="empty-main">暂无模型，点击「+ 添加模型」</span></div></td></tr>';
      return;
    }
    body.innerHTML = modelsData.map(function (m, i) {
      var paramsTxt = JSON.stringify(m.params || {});
      return (
        "<tr data-i=\"" + i + "\">" +
        '<td><input class="cell-input" data-k="name" value="' + esc(m.name) + '" placeholder="模型名"></td>' +
        '<td><input class="cell-input mini num" data-k="quota" type="number" min="1" value="' + esc(m.quota) + '"></td>' +
        '<td><input class="cell-input" data-k="desc" value="' + esc(m.desc) + '" placeholder="给LLM的说明"></td>' +
        '<td><input class="cell-input" data-k="params" value="' + esc(paramsTxt) + '" placeholder=\'{"size":"2K"}\'></td>' +
        '<td><select class="cell-input mini" data-k="sizes">' +
        SIZE_OPTIONS.map(function (o) {
          return '<option value="' + o.value + '"' + (sizesToSel(m.allowed_sizes) === o.value ? " selected" : "") + ">" + o.label + "</option>";
        }).join("") +
        "</select></td>" +
        '<td><button type="button" class="btn-icon" data-act="del" title="删除">' +
        '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button></td>' +
        "</tr>"
      );
    }).join("");
  }

  var isProviderDirty = false;
  function markProviderDirty() { isProviderDirty = true; }

  function syncModelsDataFromDOM() {
    var rows = el("modelsBody").querySelectorAll("tr[data-i]");
    rows.forEach(function (tr) {
      var i = parseInt(tr.getAttribute("data-i"), 10);
      if (isNaN(i) || !modelsData[i]) return;
      tr.querySelectorAll("[data-k]").forEach(function (inp) {
        var k = inp.getAttribute("data-k");
        var v = inp.value.trim();
        if (k === "name") modelsData[i].name = v;
        else if (k === "quota") modelsData[i].quota = parseInt(v, 10) || 1;
        else if (k === "desc") modelsData[i].desc = v;
        else if (k === "params") {
          try {
            var parsed = JSON.parse(v);
            modelsData[i].params = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
          } catch (e) {}
        } else if (k === "sizes") modelsData[i].allowed_sizes = sizesFromSel(v);
      });
    });
  }

  function collectModels() {
    syncModelsDataFromDOM();
    var rows = el("modelsBody").querySelectorAll("tr[data-i]");
    var out = [];
    var seen = {};
    var ok = true;
    rows.forEach(function (tr) {
      var m = { name: "", quota: 1, desc: "", params: {}, allowed_sizes: null };
      var hasAnyInput = false;
      tr.querySelectorAll("[data-k]").forEach(function (inp) {
        var k = inp.getAttribute("data-k");
        var v = inp.value.trim();
        if (v) hasAnyInput = true;
        if (k === "name") m.name = v;
        else if (k === "quota") m.quota = parseInt(v, 10) || 1;
        else if (k === "desc") m.desc = v;
        else if (k === "params") {
          if (!v) m.params = {};
          else {
            try {
              var parsed = JSON.parse(v);
              m.params = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
              inp.classList.remove("invalid");
            } catch (e) {
              inp.classList.add("invalid");
              ok = false;
            }
          }
        } else if (k === "sizes") m.allowed_sizes = sizesFromSel(v);
      });
      // 容错：未填模型名且无其他内容的空行（添加但未填的草稿行），自动略过，不阻断整体保存
      if (!m.name) {
        if (!hasAnyInput) return;
        ok = false;
        var nm = tr.querySelector('[data-k="name"]');
        if (nm) nm.classList.add("invalid");
        return;
      }
      if (seen[m.name]) return; // 去重
      seen[m.name] = true;
      out.push(m);
    });
    return { ok: ok, models: out };
  }

  async function loadProviderModels(providerId) {
    try {
      var data = await apiGet("providers/models", { provider: providerId });
      modelsData = (data && data.models) || [];
      renderModels();
      el("modelsNote").textContent = "已加载 " + modelsData.length + " 个模型";
    } catch (e) {
      toast("加载模型失败：" + e.message, "error");
      el("modelsBody").innerHTML =
        '<tr class="row-empty"><td colspan="6"><div class="empty"><span class="empty-main">加载失败：' + esc(e.message) + "</span></div></td></tr>";
    }
  }

  /* 统一整页保存：配置与模型同时落盘，彻底解决分卡片保存导致的未保存与状态脱节 */
  async function saveEntireProvider() {
    if (currentProviderId == null) return;
    var name = el("pdName").value.trim();
    if (!name) {
      el("pdName").classList.add("invalid");
      toast("请填写提供商名字", "error");
      return;
    }
    var r = collectModels();
    if (!r.ok) {
      toast("模型信息不合法：请检查模型名非空、参数 JSON 格式合法", "error");
      return;
    }
    var key = el("pdApiKey").value.trim();
    var url = el("pdBaseUrl").value.trim();
    var userUrl = el("pdUserUrl").value.trim();
    var btnAll = el("btnProviderSaveAll");
    var btnCfg = el("btnProviderCfgSave");
    var btnModels = el("btnModelsSave");
    if (btnAll) btnAll.disabled = true;
    if (btnCfg) btnCfg.disabled = true;
    if (btnModels) btnModels.disabled = true;
    try {
      // 1. 保存提供商连接信息
      await apiPost("providers/save", {
        id: currentProviderId, name: name, api_key: key, base_url: url,
        user_url: userUrl,
        enabled: el("pdEnabled").checked,
        use_reverse_proxy: el("pdReverseProxy").checked,
      });
      // 2. 保存模型列表
      var res = await apiPost("providers/models", { provider: currentProviderId, models: r.models });
      modelsData = r.models;
      renderModels();
      el("pdApiKey").value = ""; // 清空未保存掩码
      el("pdTitle").textContent = name;
      isProviderDirty = false;
      // 3. 同步上级 providersData 缓存
      for (var i = 0; i < providersData.length; i++) {
        if (String(providersData[i].id) === String(currentProviderId)) {
          providersData[i].name = name;
          providersData[i].base_url = url;
          providersData[i].user_url = userUrl;
          providersData[i].enabled = el("pdEnabled").checked;
          providersData[i].use_reverse_proxy = el("pdReverseProxy").checked;
          providersData[i].model_count = modelsData.length;
          break;
        }
      }
      renderProviders();
      toast("提供商「" + name + "」与 " + modelsData.length + " 个模型已保存成功", "success");
    } catch (e) {
      toast("保存失败：" + e.message, "error");
    } finally {
      if (btnAll) btnAll.disabled = false;
      if (btnCfg) btnCfg.disabled = false;
      if (btnModels) btnModels.disabled = false;
    }
  }

  async function saveProviderModels() {
    return await saveEntireProvider();
  }

  /* ---- 提供商列表视图 ---- */
  function showProvidersView() {
    providerView = "list";
    el("providersView").hidden = false;
    el("providerDetailView").hidden = true;
  }
  function showDetailView() {
    providerView = "detail";
    el("providersView").hidden = true;
    el("providerDetailView").hidden = false;
  }

  function renderProviders() {
    var wrap = el("providersList");
    if (!providersData.length) {
      wrap.innerHTML =
        '<div class="empty">' +
        '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>' +
        '<span class="empty-main">暂无提供商，点击「+ 添加提供商」</span></div>';
      return;
    }
    wrap.innerHTML = providersData.map(function (p) {
      var tm = typeMeta(p.type);
      return (
        '<article class="provider-card" data-id="' + esc(p.id) + '">' +
        '<div class="provider-card-head">' +
        '<div class="provider-card-title">' +
        '<span class="type-badge ' + tm.cls + '">' + tm.label + "</span>" +
        '<b class="provider-name">' + esc(p.name) + "</b>" +
        "</div>" +
        '<input type="checkbox" class="switch" data-act="toggle" title="启用 / 停用" aria-label="启用开关"' +
        (p.enabled ? " checked" : "") + ">" +
        "</div>" +
        '<div class="provider-meta">' +
        '<span class="pm-item"><span class="pm-k">API Key</span><code class="pm-key">' + esc(p.api_key || "") + (p.api_key ? "" : "未设置") + "</code></span>" +
        '<span class="pm-item"><span class="pm-k">BaseURL</span><code class="pm-url">' + esc(p.base_url || "—") + "</code></span>" +
        "</div>" +
        '<div class="provider-card-foot">' +
        '<span class="badge neutral">' + esc(p.model_count != null ? p.model_count : 0) + " 个模型</span>" +
        '<div class="provider-actions">' +
        '<button type="button" class="btn btn-ghost btn-sm" data-act="models">管理模型</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-act="edit">编辑</button>' +
        '<button type="button" class="btn btn-danger btn-sm" data-act="del">删除</button>' +
        "</div></div></article>"
      );
    }).join("");
  }

  async function loadProviders() {
    try {
      var data = await apiGet("providers");
      providersData = (data && data.providers) || [];
      renderProviders();
    } catch (e) {
      toast("加载提供商失败：" + e.message, "error");
      el("providersList").innerHTML =
        '<div class="empty"><span class="empty-main">加载失败：' + esc(e.message) + "</span></div>";
    }
  }

  /* ---- 提供商详情视图 ---- */
  /* 反代/UserURL 为 NovalAI 专属：按提供商类型/所选模板控制相关字段显隐 */
  function syncReverseProxyField(type) {
    var isNovalai = type === "novalai";
    el("pdReverseProxyField").hidden = !isNovalai;
    el("provReverseProxyField").hidden = !isNovalai;
    el("provUserUrlField").hidden = !isNovalai;
  }

  async function openProvider(id) {
    currentProviderId = id;
    var p = null;
    for (var i = 0; i < providersData.length; i++) {
      if (String(providersData[i].id) === String(id)) { p = providersData[i]; break; }
    }
    if (!p) { toast("找不到该提供商", "error"); return; }
    var tm = typeMeta(p.type);
    el("pdTitle").textContent = p.name || "提供商详情";
    el("pdTypeBadge").textContent = tm.label;
    el("pdTypeBadge").className = "type-badge " + tm.cls;
    el("pdName").value = p.name || "";
    el("pdApiKey").value = ""; // 留空 = 不修改
    el("pdBaseUrl").value = p.base_url || "";
    el("pdUserUrl").value = p.user_url || "";
    el("pdEnabled").checked = !!p.enabled;
    el("pdReverseProxy").checked = !!p.use_reverse_proxy;
    syncReverseProxyField(p.type);
    showDetailView();
    await loadProviderModels(id);
    isProviderDirty = false;
  }

  async function backToProviders() {
    if (isProviderDirty) {
      if (!(await confirmDialog("当前提供商有未保存的修改，确定返回吗？未保存的内容将丢失。", "未保存提示"))) return;
    }
    isProviderDirty = false;
    currentProviderId = null;
    showProvidersView();
    loadProviders(); // 刷新模型数等
  }

  async function saveProviderConfig() {
    return await saveEntireProvider();
  }

  async function toggleProvider(p, next) {
    var prev = !!p.enabled;
    p.enabled = next;
    renderProviders();
    try {
      await apiPost("providers/save", {
        id: p.id, name: p.name || "", api_key: "", base_url: p.base_url || "",
        user_url: p.user_url || "",
        enabled: next,
        use_reverse_proxy: !!p.use_reverse_proxy,
      });
      toast("提供商「" + p.name + "」已" + (next ? "启用" : "停用"), "success");
    } catch (e) {
      p.enabled = prev; // 失败回滚
      renderProviders();
      toast("切换失败：" + e.message, "error");
    }
  }

  async function deleteProvider(id) {
    var p = null;
    for (var i = 0; i < providersData.length; i++) {
      if (String(providersData[i].id) === String(id)) { p = providersData[i]; break; }
    }
    if (!p) return;
    if (!(await confirmDialog("确认删除提供商「" + p.name + "」？其下所有模型配置将一并删除。", "删除提供商"))) return;
    try {
      await apiPost("providers/delete", { id: id });
      toast("已删除提供商「" + p.name + "」", "success");
      if (String(currentProviderId) === String(id)) {
        currentProviderId = null;
        showProvidersView();
      }
      await loadProviders();
    } catch (e) {
      toast("删除失败：" + e.message, "error");
    }
  }

  /* ---- 添加 / 编辑提供商弹层 ---- */
  var providerModalMode = "add"; // "add" | "edit"
  var editProviderId = null;
  var selectedTemplateType = "gemini";

  function showAddProvider() {
    providerModalMode = "add";
    editProviderId = null;
    el("providerModalTitle").textContent = "添加提供商";
    el("templatePick").hidden = false;
    el("provEnabledField").hidden = true;
    el("provName").value = "";
    el("provKey").value = "";
    el("provUrl").value = "";
    el("provUserUrl").value = "";
    el("provReverseProxy").checked = false;
    el("provName").classList.remove("invalid");
    el("provKey").classList.remove("invalid");
    selectTemplate("gemini");
    syncReverseProxyField(selectedTemplateType);
    openModal("providerModal");
    setTimeout(function () { el("provName").focus(); }, 30);
  }

  function showEditProvider(id) {
    var p = null;
    for (var i = 0; i < providersData.length; i++) {
      if (String(providersData[i].id) === String(id)) { p = providersData[i]; break; }
    }
    if (!p) { toast("找不到该提供商", "error"); return; }
    providerModalMode = "edit";
    editProviderId = id;
    el("providerModalTitle").textContent = "编辑提供商";
    el("templatePick").hidden = true;
    el("provEnabledField").hidden = false;
    el("provName").value = p.name || "";
    el("provKey").value = ""; // 留空 = 不修改
    el("provUrl").value = p.base_url || "";
    el("provUserUrl").value = p.user_url || "";
    el("provEnabled").checked = !!p.enabled;
    el("provReverseProxy").checked = !!p.use_reverse_proxy;
    syncReverseProxyField(p.type);
    el("provName").classList.remove("invalid");
    el("provKey").classList.remove("invalid");
    openModal("providerModal");
    setTimeout(function () { el("provName").focus(); }, 30);
  }

  function selectTemplate(type) {
    selectedTemplateType = type;
    el("templateRow").querySelectorAll(".template-card").forEach(function (c) {
      c.classList.toggle("is-selected", c.getAttribute("data-type") === type);
    });
    // 仅在当前未填写或当前内容为其他模板默认地址时，才自动填充模板默认 BaseURL，防冲掉用户已粘贴的自建/反代地址
    var tpl = PROVIDER_TEMPLATES[type];
    if (tpl && tpl.base_url) {
      var curUrl = el("provUrl").value.trim();
      var isDefault = !curUrl || Object.keys(PROVIDER_TEMPLATES).some(function (k) {
        return PROVIDER_TEMPLATES[k].base_url === curUrl;
      });
      if (isDefault) {
        el("provUrl").value = tpl.base_url;
      }
    }
    // 反代模式为 NovalAI 专属：随模板切换显隐
    syncReverseProxyField(type);
  }

  async function saveProviderFromModal() {
    var name = el("provName").value.trim();
    if (!name) {
      el("provName").classList.add("invalid");
      toast("请填写提供商名字", "error");
      return;
    }
    var key = el("provKey").value.trim();
    var url = el("provUrl").value.trim();
    var userUrl = el("provUserUrl").value.trim();
    var btn = el("providerSave");
    btn.disabled = true;
    try {
      if (providerModalMode === "edit") {
        await apiPost("providers/save", {
          id: editProviderId, name: name, api_key: key, base_url: url,
          user_url: userUrl,
          enabled: el("provEnabled").checked,
          use_reverse_proxy: el("provReverseProxy").checked,
        });
        toast("提供商「" + name + "」已更新", "success");
      } else {
        await apiPost("providers", {
          name: name, type: selectedTemplateType, api_key: key, base_url: url,
          user_url: userUrl,
          use_reverse_proxy: el("provReverseProxy").checked,
        });
        toast("已添加提供商「" + name + "」", "success");
      }
      closeModal("providerModal");
      await loadProviders();
    } catch (e) {
      toast((providerModalMode === "edit" ? "更新" : "添加") + "失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /* ============================================================
   * Tab 1.5 · 生图测试台 (Workbench Studio)
   * ============================================================ */
  var wbModelsData = [];
  var wbSelectedModelObj = null;
  var wbRefImageBase64 = null;
  var wbTimerInterval = null;
  var wbLastResult = null;

  async function loadWorkbenchData() {
    try {
      var data = await apiGet("workbench/models");
      wbModelsData = (data && data.models) || [];
      renderWbModels();
      renderWbPresetsDropdown();
    } catch (e) {
      toast("加载测试台模型失败：" + e.message, "error");
    }
  }

  function renderWbModels() {
    var sel = el("wbModel");
    if (!sel) return;
    if (!wbModelsData.length) {
      sel.innerHTML = '<option value="">暂无可用模型（请先在提供商管理添加）</option>';
      onWbModelChange("");
      return;
    }
    // 按提供商名称分组
    var groups = {};
    wbModelsData.forEach(function (m) {
      var gName = m.provider_name || "默认提供商";
      if (!groups[gName]) groups[gName] = [];
      groups[gName].push(m);
    });
    var html = "";
    for (var g in groups) {
      html += '<optgroup label="' + esc(g) + '">';
      groups[g].forEach(function (m) {
        html += '<option value="' + esc(m.name) + '">' + esc(m.name) + " (" + esc(m.provider_type) + ")" + "</option>";
      });
      html += "</optgroup>";
    }
    sel.innerHTML = html;
    if (wbModelsData[0]) {
      onWbModelChange(wbModelsData[0].name);
    }
  }

  function renderWbPresetsDropdown() {
    var sel = el("wbPresetSelect");
    if (!sel) return;
    sel.innerHTML = '<option value="">选择画师预设…</option>' +
      artistPresetsData.map(function (p) {
        return '<option value="' + esc(p.name) + '">' + esc(p.name) + "</option>";
      }).join("");
  }

  function onWbModelChange(modelName) {
    wbSelectedModelObj = wbModelsData.find(function (m) { return m.name === modelName; }) || null;
    var badge = el("wbProviderBadge");
    var desc = el("wbParamsDesc");
    var naiSec = el("wbNaiSection");
    var gemSec = el("wbGeminiSection");
    var oaiSec = el("wbOpenaiSection");

    if (!wbSelectedModelObj) {
      if (badge) { badge.textContent = "未选模型"; badge.className = "badge neutral"; }
      if (naiSec) naiSec.hidden = true;
      if (gemSec) gemSec.hidden = true;
      if (oaiSec) oaiSec.hidden = true;
      return;
    }

    var pType = (wbSelectedModelObj.provider_type || "openai").toLowerCase();
    var pName = wbSelectedModelObj.provider_name || "提供商";
    if (badge) {
      badge.textContent = pName + " (" + pType.toUpperCase() + ")";
      badge.className = "badge " + (pType === "novalai" ? "accent" : (pType === "gemini" ? "success" : "neutral"));
    }

    // 自适应切换专属参数区
    if (pType === "novalai") {
      if (desc) desc.textContent = "NovelAI 专属生成参数：支持精细控制采样器、采样步数、CFG Scale 与分辨率档位。";
      if (naiSec) naiSec.hidden = false;
      if (gemSec) gemSec.hidden = true;
      if (oaiSec) oaiSec.hidden = true;
    } else if (pType === "gemini") {
      if (desc) desc.textContent = "Gemini 原生专属参数：支持画质分辨率切换、安全过滤级别与温度控制。";
      if (naiSec) naiSec.hidden = true;
      if (gemSec) gemSec.hidden = false;
      if (oaiSec) oaiSec.hidden = true;
    } else {
      if (desc) desc.textContent = "OpenAI 兼容参数：支持分辨率、Temperature 与 Max Tokens 微调。";
      if (naiSec) naiSec.hidden = true;
      if (gemSec) gemSec.hidden = true;
      if (oaiSec) oaiSec.hidden = false;
    }
  }

  function collectWbParams() {
    var pType = (wbSelectedModelObj && wbSelectedModelObj.provider_type || "openai").toLowerCase();
    var params = {};
    var imageSize = "";

    if (pType === "novalai") {
      imageSize = el("wbNaiSize").value;
      params.sampler = el("wbNaiSampler").value;
      params.steps = parseInt(el("wbNaiSteps").value, 10) || 23;
      params.scale = parseFloat(el("wbNaiScale").value) || 6.0;
      var seedVal = el("wbNaiSeed").value.trim();
      if (seedVal !== "") params.seed = parseInt(seedVal, 10);
    } else if (pType === "gemini") {
      imageSize = el("wbGeminiSize").value;
      var safety = el("wbGeminiSafety").value;
      if (safety) params.safety_level = safety;
      params.temperature = parseFloat(el("wbGeminiTemp").value) || 0.9;
    } else {
      imageSize = el("wbOpenaiSize").value;
      params.temperature = parseFloat(el("wbOpenaiTemp").value) || 1.0;
      var mt = parseInt(el("wbOpenaiMaxTokens").value, 10);
      if (!isNaN(mt) && mt > 0) params.max_tokens = mt;
    }

    return { imageSize: imageSize, params: params };
  }

  async function doWorkbenchGenerate() {
    var prompt = el("wbPrompt").value.trim();
    if (!prompt) {
      el("wbPrompt").classList.add("invalid");
      toast("请填写正向提示词 (Prompt)", "error");
      return;
    }
    el("wbPrompt").classList.remove("invalid");

    var model = el("wbModel").value;
    var neg = el("wbNegative").value.trim();
    var cfg = collectWbParams();

    var payload = {
      model: model,
      prompt: prompt,
      negative_prompt: neg,
      image_size: cfg.imageSize,
      params: cfg.params,
      reference_image: wbRefImageBase64,
    };

    // UI 切换为加载中
    var btn = el("btnWbGenerate");
    btn.disabled = true;
    el("wbEmptyState").hidden = true;
    el("wbResultView").hidden = true;
    el("wbResultActions").hidden = true;
    el("wbLoadingState").hidden = false;
    el("wbLoadingTitle").textContent = "正在向「" + (model || "默认模型") + "」发送生图请求…";

    var startTime = Date.now();
    el("wbLoadingTimer").textContent = "耗时：0.0s";
    if (wbTimerInterval) clearInterval(wbTimerInterval);
    wbTimerInterval = setInterval(function () {
      var s = ((Date.now() - startTime) / 1000).toFixed(1);
      el("wbLoadingTimer").textContent = "耗时：" + s + "s";
    }, 100);

    try {
      var res = await apiPost("workbench/generate", payload);
      wbLastResult = res;
      el("wbLoadingState").hidden = true;
      el("wbResultView").hidden = false;
      el("wbResultActions").hidden = false;
      el("wbResultImg").src = res.image_base64;
      el("wbResultMeta").textContent = "生成成功！实际模型: " + res.model + " · 用时 " + res.elapsed + "s · 大小 " + Math.round(res.size_bytes / 1024) + " KB";
      toast("生图测试完成！耗时 " + res.elapsed + "s", "success");
    } catch (e) {
      el("wbLoadingState").hidden = true;
      el("wbEmptyState").hidden = false;
      toast("生成失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
      if (wbTimerInterval) clearInterval(wbTimerInterval);
    }
  }

  function downloadWbImage() {
    if (!wbLastResult || !wbLastResult.image_base64) return;
    var a = document.createElement("a");
    a.href = wbLastResult.image_base64;
    var now = new Date();
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var ts = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "_" + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    a.download = "workbench_" + (wbLastResult.model || "gen") + "_" + ts + ".png";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast("已开始下载生成图片", "success");
  }

  function resetWbForm() {
    el("wbPrompt").value = "";
    el("wbNegative").value = "";
    clearWbRefImage();
    el("wbNaiSteps").value = 23;
    el("wbNaiStepsVal").textContent = "23";
    el("wbNaiScale").value = 6.0;
    el("wbNaiScaleVal").textContent = "6.0";
    el("wbNaiSeed").value = "";
    el("wbGeminiTemp").value = 0.9;
    el("wbGeminiTempVal").textContent = "0.9";
    el("wbOpenaiTemp").value = 1.0;
    el("wbOpenaiTempVal").textContent = "1.0";
    el("wbEmptyState").hidden = false;
    el("wbResultView").hidden = true;
    el("wbResultActions").hidden = true;
    toast("测试台参数已重置", "info");
  }

  function clearWbRefImage() {
    wbRefImageBase64 = null;
    el("wbRefFileInput").value = "";
    el("wbRefEmptyView").hidden = false;
    el("wbRefPreviewView").hidden = true;
    el("btnWbClearRef").hidden = true;
  }

  function loadLogToWorkbench(ts) {
    var r = logItems.find(function (it) { return it.timestamp === ts; });
    if (!r) return;
    switchPage("workbench");
    setTimeout(function () {
      if (r.prompt) el("wbPrompt").value = r.prompt;
      if (r.model && el("wbModel")) {
        el("wbModel").value = r.model;
        onWbModelChange(r.model);
      }
      toast("已从日志载入提示词与模型到测试台", "info");
    }, 100);
  }

  /* ============================================================
   * Tab 2 · 次数管理
   * ============================================================ */
  var countKind = "users"; // users | groups
  var countPage = 1;
  var countTotal = 0;
  var countSearch = "";
  var PAGE_SIZE = 10;
  var rechargeTarget = null; // {kind, id, label}
  var setTarget = null;      // {kind, id, label}

  async function loadCounts(reset) {
    if (reset) countPage = 1;
    var endpoint = countKind === "users" ? "users" : "groups";
    try {
      var data = await apiGet(endpoint, { search: countSearch, page: countPage, page_size: PAGE_SIZE });
      var items = (data && (data.users || data.groups)) || [];
      countTotal = (data && data.total) || 0;
      renderCounts(items);
    } catch (e) {
      toast("加载次数失败：" + e.message, "error");
      el("countsBody").innerHTML =
        '<tr class="row-empty"><td colspan="3"><div class="empty"><span class="empty-main">加载失败：' + esc(e.message) + "</span></div></td></tr>";
    }
  }

  function renderCounts(items) {
    var body = el("countsBody");
    el("countIdHead").textContent = countKind === "users" ? "QQ 号" : "群号";
    if (!items.length) {
      body.innerHTML =
        '<tr class="row-empty"><td colspan="3"><div class="empty">' +
        '<span class="empty-main">暂无记录' + (countSearch ? "，换个关键词试试" : "") + "</span></div></td></tr>";
    } else {
      body.innerHTML = items.map(function (it) {
        var id = countKind === "users" ? it.qq : it.group_id;
        return (
          "<tr>" +
          '<td><b>' + esc(id) + "</b></td>" +
          '<td class="num"><span class="badge neutral">' + esc(it.count) + " 次</span></td>" +
          '<td><button type="button" class="btn btn-ghost btn-sm" data-act="recharge" data-id="' + esc(id) + '">充值</button>' +
          '<button type="button" class="btn btn-ghost btn-sm" data-act="set" data-id="' + esc(id) + '" data-count="' + esc(it.count) + '">设置</button></td>' +
          "</tr>"
        );
      }).join("");
    }
    var totalPages = Math.max(1, Math.ceil(countTotal / PAGE_SIZE));
    el("countPageInfo").textContent = "第 " + countPage + " / " + totalPages + " 页";
    el("countPrev").disabled = countPage <= 1;
    el("countNext").disabled = countPage >= totalPages;
    el("countMeta").textContent = "共 " + countTotal + " 条";
  }

  function openRecharge(id, label) {
    setTarget = null;
    rechargeTarget = { kind: countKind, id: id, label: label };
    el("rechargeTitle").textContent = "充值次数";
    el("rechargeDesc").textContent = "目标：" + (countKind === "users" ? "用户 " : "群 ") + label + " · 充值为追加数量";
    el("rechargeAmount").value = "";
    el("rechargeAmount").setAttribute("min", "1");
    el("rechargeAmount").setAttribute("placeholder", "例如 50");
    el("rechargeAmount").classList.remove("invalid");
    el("rechargeChips").hidden = false;
    el("rechargeChips").querySelectorAll(".chip").forEach(function (c) { c.classList.remove("is-selected"); });
    openModal("rechargeModal");
    setTimeout(function () { el("rechargeAmount").focus(); }, 30);
  }

  function openSet(id, label, currentCount) {
    rechargeTarget = null;
    setTarget = { kind: countKind, id: id, label: label };
    el("rechargeTitle").textContent = "设置次数";
    el("rechargeDesc").textContent = "目标：" + (countKind === "users" ? "用户 " : "群 ") + label +
      " · 当前 " + (currentCount == null ? "?" : currentCount) + " 次，设置为指定值（0 = 清零）";
    el("rechargeAmount").value = "";
    el("rechargeAmount").setAttribute("min", "0");
    el("rechargeAmount").setAttribute("placeholder", "目标次数（非负整数）");
    el("rechargeAmount").classList.remove("invalid");
    el("rechargeChips").hidden = true;
    openModal("rechargeModal");
    setTimeout(function () { el("rechargeAmount").focus(); }, 30);
  }

  async function confirmRecharge() {
    if (setTarget) {
      // 设置模式：直接设置具体次数
      var scount = parseInt(el("rechargeAmount").value, 10);
      if (isNaN(scount) || scount < 0) {
        el("rechargeAmount").classList.add("invalid");
        toast("请输入非负整数", "error");
        return;
      }
      el("rechargeAmount").classList.remove("invalid");
      var sbtn = el("rechargeConfirm");
      sbtn.disabled = true;
      try {
        var sbody = { count: scount };
        if (setTarget.kind === "users") {
          sbody.qq = setTarget.id;
          await apiPost("users/set", sbody);
        } else {
          sbody.group_id = setTarget.id;
          await apiPost("groups/set", sbody);
        }
        toast((setTarget.kind === "users" ? "用户 " : "群 ") + setTarget.label + " 次数已设置为 " + scount, "success");
        closeModal("rechargeModal");
        loadCounts(true);
      } catch (e) {
        toast("设置失败：" + e.message, "error");
      } finally {
        sbtn.disabled = false;
      }
      return;
    }
    if (!rechargeTarget) return;
    var amount = parseInt(el("rechargeAmount").value, 10);
    if (isNaN(amount) || amount === 0) {
      el("rechargeAmount").classList.add("invalid");
      toast("请输入非零整数", "error");
      return;
    }
    // 后端 users/add / groups/add 的 amount 要求 >= 1，仅支持充值，不支持负数扣减
    if (amount < 0) {
      el("rechargeAmount").classList.add("invalid");
      toast("当前充值接口仅支持正整数（扣减请用「设置」设为目标值）", "error");
      return;
    }
    el("rechargeAmount").classList.remove("invalid");
    var btn = el("rechargeConfirm");
    btn.disabled = true;
    try {
      var body = { amount: amount };
      if (rechargeTarget.kind === "users") {
        body.qq = rechargeTarget.id;
        var u = await apiPost("users/add", body);
        toast("用户 " + rechargeTarget.label + " 次数已更新为 " + (u && u.count), "success");
      } else {
        body.group_id = rechargeTarget.id;
        var g = await apiPost("groups/add", body);
        toast("群 " + rechargeTarget.label + " 次数已更新为 " + (g && g.count), "success");
      }
      closeModal("rechargeModal");
      loadCounts(true);
    } catch (e) {
      toast("充值失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /* ============================================================
   * Tab 3 · 生图日志
   * ============================================================ */
  var logPage = 1;
  var logTotal = 0;
  var logItems = [];
  var logLimit = 20;
  var groupOptions = [];

  function logScope() { return el("logScope").value || "全局"; }
  function logDays() { return parseInt(el("logDays").value, 10) || 0; }

  async function loadLogs(reset) {
    if (reset) { logPage = 1; logItems = []; }
    var scope = logScope();
    var params = { scope: scope, days: logDays(), limit: logLimit, page: logPage };
    if (scope === "本群") {
      // 本群需指定具体群号（WebUI 无会话群上下文）；未选群时回退为全局避免空结果
      if (el("logGroup").value) {
        params.group_id = el("logGroup").value;
      } else {
        params.scope = "全局";
      }
    }
    try {
      var data = await apiGet("logs", params);
      var logs = (data && data.logs) || [];
      logTotal = (data && data.total) || 0;
      if (logPage === 1) logItems = logs; else logItems = logItems.concat(logs);
      renderLogs();
    } catch (e) {
      toast("加载日志失败：" + e.message, "error");
      el("logsBody").innerHTML =
        '<div class="empty"><span class="empty-main">加载失败：' + esc(e.message) + "</span></div>";
    }
  }

  function renderLogs() {
    var wrap = el("logsBody");
    if (!logItems.length) {
      wrap.innerHTML =
        '<div class="empty">' +
        '<svg viewBox="0 0 24 24"><path d="M4 5h16M4 12h16M4 19h10"/></svg>' +
        '<span class="empty-main">暂无生图记录</span></div>';
      el("logsMore").disabled = true;
      el("logsMeta").textContent = "共 0 条";
      return;
    }
    wrap.innerHTML = logItems.map(function (r) {
      // 缩略图：data-path 存文件路径，交给懒加载器（滚动到可见才请求图片，避免一次请求全部）
      return (
        '<article class="log-card" data-ts="' + esc(r.timestamp) + '">' +
        '<div class="thumb" data-path="' + esc(r.file_path || "") + '">' +
        '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>' +
        '<span class="thumb-time">' + esc(shortTs(r.timestamp)) + "</span>" +
        "</div>" +
        '<div class="log-main">' +
        '<div class="log-meta">' +
        '<span>时间 <b>' + esc(fmtLogTime(r.timestamp)) + "</b></span>" +
        '<span>群 <b>' + esc(r.group_id || "private") + "</b></span>" +
        '<span>请求者 <b>' + esc(r.user_id || "?") + "</b></span>" +
        '</div><p class="log-prompt">' + esc(r.prompt || "") + "</p>" +
        '<span class="log-model">模型：' + esc(r.model || "?") + "</span>" +
        "</div>" +
        '<div class="log-actions">' +
        '<button type="button" class="btn btn-ghost btn-sm" data-act="load-wb">载入测试台</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-act="view">查看大图</button>' +
        '<button type="button" class="btn btn-danger btn-sm" data-act="del">删除</button>' +
        "</div>" +
        "</article>"
      );
    }).join("");
    scheduleThumbLoad();
    var more = logItems.length < logTotal;
    el("logsMore").disabled = !more;
    el("logsMore").textContent = more ? "加载更多" : "已加载全部";
    el("logsMeta").textContent = "共 " + logTotal + " 条 · 已加载 " + logItems.length;
  }

  /* 缩略图懒加载：用 IntersectionObserver，滚动到可见才取图；
     无 IntersectionObserver 时降级为对新渲染的缩略图全部直接加载（避免「加载更多」后新图不加载）。 */
  var thumbObserver = null;
  function scheduleThumbLoad() {
    var thumbs = Array.prototype.slice.call(el("logsBody").querySelectorAll(".thumb[data-path]"));
    if (!thumbs.length) return;
    if (!("IntersectionObserver" in window)) {
      thumbs.forEach(loadThumb);
      return;
    }
    if (!thumbObserver) {
      thumbObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            thumbObserver.unobserve(entry.target);
            loadThumb(entry.target);
          }
        });
      }, { rootMargin: "120px" });
    }
    thumbs.forEach(function (t) { thumbObserver.observe(t); });
  }

  function loadThumb(thumbEl) {
    var path = thumbEl.getAttribute("data-path");
    thumbEl.removeAttribute("data-path");
    if (!path) return; // 无路径保持占位
    apiGet("logs/image", { path: path }).then(function (res) {
      if (res && res.image) {
        // 用 createElement 而非 innerHTML 拼接，避免把 data URL 直接插入 HTML 字符串
        thumbEl.innerHTML = "";
        var img = document.createElement("img");
        img.className = "thumb-img";
        img.alt = "生图缩略图";
        img.src = res.image;
        thumbEl.appendChild(img);
      }
      // 失败/无图保持占位，不崩
    }).catch(function () {
      /* 保持占位 */
    });
  }

  function viewLogDetail(ts) {
    var r = null;
    for (var i = 0; i < logItems.length; i++) {
      if (String(logItems[i].timestamp) === String(ts)) { r = logItems[i]; break; }
    }
    if (!r) return;
    var rows = [
      ["时间", fmtLogTime(r.timestamp)],
      ["群号", r.group_id || "private"],
      ["请求者", r.user_id || "?"],
      ["模型", r.model || "?"],
      ["文件路径", r.file_path || "（无）"],
      ["完整提示词", r.prompt || "（空）"],
    ];
    el("viewDetail").innerHTML = rows.map(function (kv) {
      return '<div class="dl-item"><span class="dl-k">' + esc(kv[0]) + '</span><span class="dl-v">' + esc(kv[1]) + "</span></div>";
    }).join("");
    // 加载真实图片（backend logs/image 返回 base64 data URL）
    var imgWrap = el("viewImageWrap"), img = el("viewImage"), ph = el("viewPlaceholder");
    imgWrap.hidden = true;
    ph.hidden = false;
    ph.textContent = "🖼 正在加载图片…";
    if (!r.file_path) {
      ph.textContent = "🖼 该记录无图片文件";
    } else {
      apiGet("logs/image", { path: r.file_path }).then(function (res) {
        if (res && res.image) {
          img.src = res.image;
          imgWrap.hidden = false;
          ph.hidden = true;
        } else {
          ph.textContent = "🖼 图片加载失败或文件不存在";
        }
      }).catch(function (e) {
        ph.textContent = "🖼 图片加载失败：" + e.message;
      });
    }
    openModal("viewModal");
  }

  async function deleteLog(ts) {
    try {
      var res = await apiPost("logs/delete", { timestamps: [String(ts)] });
      toast("已删除 " + (res && res.deleted != null ? res.deleted : 1) + " 条记录", "success");
      logItems = logItems.filter(function (r) { return String(r.timestamp) !== String(ts); });
      loadLogs(true);
    } catch (e) {
      toast("删除失败：" + e.message, "error");
    }
  }

  async function loadGroupOptions() {
    try {
      var data = await apiGet("groups", { page: 1, page_size: 200 });
      var gs = (data && data.groups) || [];
      groupOptions = gs.map(function (g) { return g.group_id; });
      el("logGroup").innerHTML =
        '<option value="">全部群</option>' +
        groupOptions.map(function (g) { return '<option value="' + esc(g) + '">' + esc(g) + "</option>"; }).join("");
    } catch (e) { /* 群列表加载失败不阻塞日志 */ }
  }

  /* ============================================================
   * 画师预设（设置页内）
   * ============================================================ */
  var artistPresetsData = []; // [{name, artists, negative}]
  var artistGroupsData = {};  // {"群ID": "预设名"}

  function syncArtistGroupPresetOptions() {
    var sel = el("artistGroupPreset");
    var cur = sel.value;
    sel.innerHTML =
      '<option value="">（选择预设）</option>' +
      artistPresetsData.map(function (p) {
        return '<option value="' + esc(p.name) + '">' + esc(p.name) + "</option>";
      }).join("");
    // 尽量保留原选择；若已不存在回退到第一个
    if (cur && artistPresetsData.some(function (p) { return p.name === cur; })) {
      sel.value = cur;
    } else if (artistPresetsData.length) {
      sel.value = artistPresetsData[0].name;
    }
  }

  function renderArtistPresets() {
    var body = el("artistPresetsBody");
    if (!artistPresetsData.length) {
      body.innerHTML =
        '<tr class="row-empty"><td colspan="4"><div class="empty">' +
        '<span class="empty-main">暂无预设，点击「+ 添加预设」</span></div></td></tr>';
      return;
    }
    body.innerHTML = artistPresetsData.map(function (p, i) {
      return (
        "<tr data-i=\"" + i + "\">" +
        '<td><input class="cell-input" data-k="name" value="' + esc(p.name) + '" placeholder="预设名"></td>' +
        '<td><input class="cell-input" data-k="artists" value="' + esc(p.artists) + '" placeholder="artist:xxx, artist:yyy"></td>' +
        '<td><input class="cell-input" data-k="negative" value="' + esc(p.negative) + '" placeholder="负面词（可空）"></td>' +
        '<td><button type="button" class="btn-icon" data-act="del" title="删除">' +
        '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button></td>' +
        "</tr>"
      );
    }).join("");
  }

  function collectArtistPresets() {
    var rows = el("artistPresetsBody").querySelectorAll("tr[data-i]");
    var out = [];
    var seen = {};
    var ok = true;
    rows.forEach(function (tr) {
      var p = { name: "", artists: "", negative: "" };
      tr.querySelectorAll("[data-k]").forEach(function (inp) {
        var k = inp.getAttribute("data-k");
        var v = inp.value.trim();
        if (k === "name") p.name = v;
        else if (k === "artists") p.artists = v;
        else if (k === "negative") p.negative = v;
      });
      if (!p.name || !p.artists) {
        ok = false;
        var nm = tr.querySelector('[data-k="name"]');
        var ar = tr.querySelector('[data-k="artists"]');
        if (nm && !nm.value.trim()) nm.classList.add("invalid");
        if (ar && !ar.value.trim()) ar.classList.add("invalid");
        return;
      }
      if (seen[p.name]) return; // 去重
      seen[p.name] = true;
      out.push(p);
    });
    return { ok: ok, presets: out };
  }

  async function loadArtistPresets() {
    try {
      var data = await apiGet("artist-presets");
      artistPresetsData = (data && data.presets) || [];
      renderArtistPresets();
      syncArtistGroupPresetOptions();
    } catch (e) {
      toast("加载画师预设失败：" + e.message, "error");
      el("artistPresetsBody").innerHTML =
        '<tr class="row-empty"><td colspan="4"><div class="empty"><span class="empty-main">加载失败：' + esc(e.message) + "</span></div></td></tr>";
    }
  }

  async function saveArtistPresets() {
    var r = collectArtistPresets();
    if (!r.ok) { toast("预设不合法：预设名与画师串不能为空", "error"); return; }
    var btn = el("btnArtistPresetsSave");
    btn.disabled = true;
    try {
      var res = await apiPost("artist-presets", { presets: r.presets });
      artistPresetsData = r.presets;
      renderArtistPresets();
      syncArtistGroupPresetOptions();
      toast("已保存 " + (res && res.count != null ? res.count : r.presets.length) + " 个预设", "success");
    } catch (e) {
      toast("保存失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  function renderArtistGroups() {
    var wrap = el("artistGroupMap");
    var entries = Object.keys(artistGroupsData);
    if (!entries.length) {
      wrap.innerHTML = '<div class="empty"><span class="empty-main">暂无按群配置</span></div>';
      return;
    }
    wrap.innerHTML = entries.map(function (gid) {
      return (
        '<div class="group-map-item" data-gid="' + esc(gid) + '">' +
        '<span class="badge neutral">群 ' + esc(gid) + "</span>" +
        '<span class="gm-preset">' + esc(artistGroupsData[gid]) + "</span>" +
        '<button type="button" class="btn btn-danger btn-sm" data-act="off">关闭</button>' +
        "</div>"
      );
    }).join("");
  }

  async function loadArtistGroups() {
    try {
      var data = await apiGet("artist-presets/group");
      artistGroupsData = (data && data.groups) || {};
      renderArtistGroups();
    } catch (e) {
      toast("加载按群画师预设失败：" + e.message, "error");
    }
  }

  async function saveArtistGroup(preset) {
    var gid = el("artistGroupId").value.trim();
    if (!gid) { toast("请填写群 ID", "error"); return; }
    var btn = el("btnArtistGroupSave");
    btn.disabled = true;
    try {
      await apiPost("artist-presets/group", { group_id: gid, preset: preset });
      toast(preset ? "已为群 " + gid + " 启用「" + preset + "」" : "已关闭群 " + gid + " 的画师预设", "success");
      el("artistGroupId").value = "";
      await loadArtistGroups();
    } catch (e) {
      toast("设置失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /* 画师预设导出与导入 / 夺舍幻笔 */
  async function exportArtistPresets() {
    try {
      var data = await apiGet("artist-presets/export");
      var jsonStr = JSON.stringify(data, null, 2);
      var blob = new Blob([jsonStr], { type: "application/json;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      var now = new Date();
      var pad = function (n) { return (n < 10 ? "0" : "") + n; };
      var ts = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "_" + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
      a.href = url;
      a.download = "rainnya_artist_presets_" + ts + ".json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast("已导出 " + ((data && data.presets && data.presets.length) || 0) + " 个画师预设", "success");
    } catch (e) {
      toast("导出失败：" + e.message, "error");
    }
  }

  var detectedNaiPresets = null;

  async function openArtistImportModal() {
    el("artistImportText").value = "";
    el("importFileTip").textContent = "未选择文件";
    el("artistImportFileInput").value = "";
    el("artistDetectBanner").hidden = true;
    detectedNaiPresets = null;
    openModal("artistImportModal");
    // 异步探测本地已安装的 AstrBot 幻笔插件
    try {
      var det = await apiGet("artist-presets/detect-nai-brush");
      if (det && det.found && det.count > 0) {
        detectedNaiPresets = det.presets;
        el("artistDetectTitle").textContent = "⚡ 检测到本地已安装 AstrBot 幻笔插件（发现 " + det.count + " 个预设）";
        el("artistDetectDesc").textContent = "配置文件：" + det.path + "，点击右侧按钮直接一键夺舍导入";
        el("artistDetectBanner").hidden = false;
      }
    } catch (e) {}
  }

  async function takeoverNaiBrush() {
    if (!detectedNaiPresets || !detectedNaiPresets.length) return;
    var mode = (document.querySelector('input[name="artistImportMode"]:checked') || {}).value || "merge";
    if (mode === "overwrite" && !(await confirmDialog("全量覆盖将清空您现有的所有画师预设，确定要继续夺舍覆盖吗？", "夺舍确认"))) return;
    try {
      var res = await apiPost("artist-presets/import", { data: detectedNaiPresets, mode: mode });
      closeModal("artistImportModal");
      await loadArtistPresets();
      toast("夺舍成功！已从幻笔插件导入 " + res.imported_count + " 个预设（当前共 " + res.total_count + " 个）", "success");
    } catch (e) {
      toast("夺舍失败：" + e.message, "error");
    }
  }

  async function doImportArtistPresets() {
    var text = el("artistImportText").value.trim();
    if (!text) {
      toast("请先选择本地 JSON 文件或粘贴预设内容", "error");
      return;
    }
    var parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      toast("JSON 格式错误，请检查文本是否为合法 JSON", "error");
      return;
    }
    var mode = (document.querySelector('input[name="artistImportMode"]:checked') || {}).value || "merge";
    if (mode === "overwrite" && !(await confirmDialog("全量覆盖将清空您现有的所有画师预设，确定要继续覆盖吗？", "导入确认"))) return;
    var btn = el("artistImportConfirm");
    btn.disabled = true;
    try {
      var res = await apiPost("artist-presets/import", { data: parsed, mode: mode });
      closeModal("artistImportModal");
      await loadArtistPresets();
      toast("导入成功！从【" + (res.source_type || "JSON") + "】导入 " + res.imported_count + " 个预设（共 " + res.total_count + " 个）", "success");
    } catch (e) {
      toast("导入失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /* ============================================================
   * 设置分栏 Tab & 预设子 Tab 控制
   * ============================================================ */
  var CFG_TAB_CURRENT = "tab-params"; // "tab-params" | "tab-defense" | "tab-enhancement" | "tab-base"
  var SUBTAB_CURRENT = "artist-subtab"; // "artist-subtab" | "prompt-subtab"

  // 字段所属分栏映射表（用于校验失败时自动定位并切换到对应的 Tab）
  var FIELD_TAB_MAP = {
    // 参数设定
    openai_size: "tab-params", openai_temperature: "tab-params", openai_max_tokens: "tab-params",
    safety_level: "tab-params", image_size: "tab-params", hires_whitelist: "tab-params",
    novalai_size: "tab-params", novalai_sampler: "tab-params", novalai_steps: "tab-params",
    novalai_scale: "tab-params", novalai_negative_prompt: "tab-params",
    novai_precheck_enabled: "tab-params", novai_precheck_timeout: "tab-params", novai_point_warn_threshold: "tab-params",
    // 绘图防护
    auto_recall_enabled: "tab-defense", auto_recall_delay: "tab-defense", auto_recall_allowed_groups: "tab-defense",
    nsfw_filter_enabled: "tab-defense", nsfw_filter_tags: "tab-defense", nsfw_zip_enabled: "tab-defense",
    // 增强模块
    tagger_provider_id: "tab-enhancement", tagger_temperature: "tab-enhancement", tagger_max_tokens: "tab-enhancement",
    artist_recommend_enabled: "tab-enhancement", artist_recommend_max_candidates: "tab-enhancement", artist_recommend_min_match: "tab-enhancement",
    selfie_prompt: "tab-enhancement",
    // 基础设定
    prefix: "tab-base", extra_prefix: "tab-base", use_proxy: "tab-base", proxy_url: "tab-base",
    download_timeout: "tab-base", download_retries: "tab-base", api_timeout: "tab-base",
    enable_user_limit: "tab-base", enable_group_limit: "tab-base", user_count_cap: "tab-base", group_task_limit: "tab-base",
    enable_checkin: "tab-base", checkin_fixed_reward: "tab-base", enable_random_checkin: "tab-base",
    checkin_random_reward_min: "tab-base", checkin_random_reward_max: "tab-base",
    user_whitelist: "tab-base", user_blacklist: "tab-base", group_whitelist: "tab-base", group_blacklist: "tab-base",
    help_menu: "tab-base"
  };

  // 字段与 DOM 元素 ID 映射表
  var FIELD_ELEMENT_MAP = {
    openai_size: "cfgOpenaiSize", openai_temperature: "cfgOpenaiTemp", openai_max_tokens: "cfgOpenaiMaxTokens",
    safety_level: "cfgSafety", image_size: "cfgImageSize", hires_whitelist: "cfgHires",
    novalai_size: "cfgNovalaiSize", novalai_sampler: "cfgNovalaiSampler", novalai_steps: "cfgNovalaiSteps",
    novalai_scale: "cfgNovalaiScale", novalai_negative_prompt: "cfgNovalaiNeg",
    novai_precheck_enabled: "cfgNovaiPrecheck", novai_precheck_timeout: "cfgNovaiPrecheckTimeout", novai_point_warn_threshold: "cfgNovaiWarn",
    auto_recall_enabled: "cfgAutoRecall", auto_recall_delay: "cfgAutoRecallDelay", auto_recall_allowed_groups: "cfgAutoRecallGroups",
    nsfw_filter_enabled: "cfgNsfwFilter", nsfw_filter_tags: "cfgNsfwTags", nsfw_zip_enabled: "cfgNsfwZipEnabled",
    tagger_provider_id: "cfgTaggerProvider", tagger_temperature: "cfgTaggerTemp", tagger_max_tokens: "cfgTaggerMaxTokens",
    artist_recommend_enabled: "cfgArtistRecEnabled", artist_recommend_max_candidates: "cfgArtistRecMax", artist_recommend_min_match: "cfgArtistRecMin",
    selfie_prompt: "cfgSelfiePrompt",
    prefix: "cfgPrefix", extra_prefix: "cfgExtraPrefix", use_proxy: "cfgUseProxy", proxy_url: "cfgProxyUrl",
    download_timeout: "cfgDownloadTimeout", download_retries: "cfgDownloadRetries", api_timeout: "cfgApiTimeout",
    enable_user_limit: "cfgEnableUser", enable_group_limit: "cfgEnableGroup", user_count_cap: "cfgUserCap", group_task_limit: "cfgTaskLimit",
    enable_checkin: "cfgEnableCheckin", checkin_fixed_reward: "cfgCheckinFixed", enable_random_checkin: "cfgRandomCheckin",
    checkin_random_reward_min: "cfgCheckinMin", checkin_random_reward_max: "cfgCheckinMax",
    user_whitelist: "cfgUserWhite", user_blacklist: "cfgUserBlack", group_whitelist: "cfgGroupWhite", group_blacklist: "cfgGroupBlack",
    help_menu: "cfgHelpMenu", prompt_list: "cfgPromptList"
  };

  function switchConfigTab(tabId) {
    CFG_TAB_CURRENT = tabId;
    document.querySelectorAll(".sub-tab-btn[data-cfgtab]").forEach(function (btn) {
      var active = btn.getAttribute("data-cfgtab") === tabId;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".cfg-tab-panel").forEach(function (p) {
      p.hidden = p.id !== "panel-" + tabId;
    });
  }

  function switchPresetSubTab(subtabId) {
    SUBTAB_CURRENT = subtabId;
    document.querySelectorAll(".sub-tab-btn[data-subtab]").forEach(function (btn) {
      var active = btn.getAttribute("data-subtab") === subtabId;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    el("panel-artist-subtab").hidden = (subtabId !== "artist-subtab");
    el("panel-prompt-subtab").hidden = (subtabId !== "prompt-subtab");
  }

  function clearFieldErrors() {
    document.querySelectorAll(".input.is-invalid").forEach(function (input) {
      input.classList.remove("is-invalid");
    });
  }

  function highlightFieldError(fieldKey, errorMsg) {
    clearFieldErrors();
    var tabId = FIELD_TAB_MAP[fieldKey];
    if (tabId) {
      switchConfigTab(tabId);
    }
    var elemId = FIELD_ELEMENT_MAP[fieldKey];
    var elem = elemId ? el(elemId) : null;
    if (elem) {
      elem.classList.add("is-invalid");
      elem.focus();
      if (elem.scrollIntoView) {
        elem.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }
    toast(errorMsg, "error");
  }

  /* ============================================================
   * Tab 4 · 基础设置
   * ============================================================ */
  async function loadConfig() {
    try {
      var c = await apiGet("config");
      /* 基础 */
      el("cfgPrefix").checked = !!c.prefix;
      el("cfgExtraPrefix").value = c.extra_prefix || "";
      el("cfgUseProxy").checked = !!c.use_proxy;
      el("cfgProxyUrl").value = c.proxy_url || "";
      el("cfgDownloadTimeout").value = c.download_timeout != null ? c.download_timeout : 60;
      el("cfgDownloadRetries").value = c.download_retries != null ? c.download_retries : 3;
      el("cfgApiTimeout").value = c.api_timeout != null ? c.api_timeout : 120;
      /* OpenAI */
      el("cfgOpenaiSize").value = (c.openai_size || "").toUpperCase();
      el("cfgOpenaiTemp").value = c.openai_temperature != null ? c.openai_temperature : "";
      el("cfgOpenaiMaxTokens").value = c.openai_max_tokens != null ? c.openai_max_tokens : "";
      /* Gemini */
      el("cfgSafety").value = c.safety_level || "";
      el("cfgImageSize").value = (c.image_size || "").toUpperCase();
      el("cfgHires").value = (c.hires_whitelist || []).join("\n");
      /* NovalAI */
      el("cfgNovalaiSize").value = c.novalai_size || "portrait_medium";
      var sampler = c.novalai_sampler || "";
      var samplerSel = el("cfgNovalaiSampler");
      samplerSel.value = sampler;
      if (sampler && samplerSel.value !== sampler) {
        // 后端返回了非预设的采样器值时，追加为自定义选项避免被丢弃
        var opt = document.createElement("option");
        opt.value = sampler;
        opt.textContent = sampler;
        samplerSel.appendChild(opt);
        samplerSel.value = sampler;
      }
      el("cfgNovalaiSteps").value = c.novalai_steps != null ? c.novalai_steps : "";
      el("cfgNovalaiScale").value = c.novalai_scale != null ? c.novalai_scale : "";
      el("cfgNovalaiNeg").value = c.novalai_negative_prompt || "";
      /* NovelAI 预检 */
      el("cfgNovaiPrecheck").checked = !!c.novai_precheck_enabled;
      el("cfgNovaiPrecheckTimeout").value = c.novai_precheck_timeout != null ? c.novai_precheck_timeout : 10;
      el("cfgNovaiWarn").value = c.novai_point_warn_threshold != null ? c.novai_point_warn_threshold : 10;
      /* 自动撤回 */
      el("cfgAutoRecall").checked = !!c.auto_recall_enabled;
      el("cfgAutoRecallDelay").value = c.auto_recall_delay != null ? c.auto_recall_delay : 5;
      el("cfgAutoRecallGroups").value = (c.auto_recall_allowed_groups || []).join("\n");
      /* NSFW 过滤 */
      el("cfgNsfwFilter").checked = !!c.nsfw_filter_enabled;
      el("cfgNsfwTags").value = c.nsfw_filter_tags || "";
      el("cfgNsfwZipEnabled").checked = c.nsfw_zip_enabled !== false;
      /* 打标 */
      el("cfgTaggerProvider").value = c.tagger_provider_id || "";
      el("cfgTaggerTemp").value = c.tagger_temperature != null ? c.tagger_temperature : "";
      el("cfgTaggerMaxTokens").value = c.tagger_max_tokens != null ? c.tagger_max_tokens : "";
      /* 画师推荐 */
      el("cfgArtistRecEnabled").checked = !!c.artist_recommend_enabled;
      el("cfgArtistRecMax").value = c.artist_recommend_max_candidates != null ? c.artist_recommend_max_candidates : "";
      el("cfgArtistRecMin").value = c.artist_recommend_min_match != null ? c.artist_recommend_min_match : "";
      /* 自拍特征 */
      el("cfgSelfiePrompt").value = c.selfie_prompt || "";
      /* 权限 */
      el("cfgUserWhite").value = (c.user_whitelist || []).join("\n");
      el("cfgUserBlack").value = (c.user_blacklist || []).join("\n");
      el("cfgGroupWhite").value = (c.group_whitelist || []).join("\n");
      el("cfgGroupBlack").value = (c.group_blacklist || []).join("\n");
      /* 次数限制 */
      el("cfgEnableUser").checked = c.enable_user_limit !== false;
      el("cfgEnableGroup").checked = !!c.enable_group_limit;
      el("cfgUserCap").value = c.user_count_cap || 0;
      el("cfgTaskLimit").value = c.group_task_limit != null ? c.group_task_limit : 2;
      /* 签到系统 */
      el("cfgEnableCheckin").checked = !!c.enable_checkin;
      el("cfgCheckinFixed").value = c.checkin_fixed_reward != null ? c.checkin_fixed_reward : 3;
      el("cfgRandomCheckin").checked = !!c.enable_random_checkin;
      el("cfgCheckinMin").value = c.checkin_random_reward_min != null ? c.checkin_random_reward_min : 1;
      el("cfgCheckinMax").value = c.checkin_random_reward_max != null ? c.checkin_random_reward_max : 5;
      /* 帮助与预设 */
      el("cfgHelpMenu").value = c.help_menu || "";
      el("cfgPromptList").value = (c.prompt_list || []).join("\n");
    } catch (e) {
      toast("加载配置失败：" + e.message, "error");
    }
  }

  function splitLines(v) {
    return String(v || "").split(/\r?\n/).map(function (s) { return s.trim(); }).filter(function (s) { return s; });
  }

  /* 逐条解析 prompt_list：按「触发词:」行首前缀识别每条，value 取到下一触发词前的全部文本（保留内部换行），
     避免 splitLines 按行切碎跨行提示词导致「无冒号碎片」报错 */
  function parsePromptList(text) {
    var s = String(text || "");
    var out = [];
    var re = /(?:^|\n)([^\n:]+):/g;
    var m = null;
    var prev = null; // {name, start} start 为上一触发词冒号后的位置
    while ((m = re.exec(s)) !== null) {
      if (prev) {
        out.push(prev.name + ":" + s.slice(prev.start, m.index));
      }
      prev = { name: m[1].trim(), start: re.lastIndex };
    }
    if (prev) {
      out.push(prev.name + ":" + s.slice(prev.start));
    }
    return out.filter(function (p) { return p.indexOf(":") > 0; });
  }

  // 客户端友好校验规则
  function validateConfigBeforeSave() {
    clearFieldErrors();

    // 1. 基础设定校验
    if (el("cfgUseProxy").checked && !el("cfgProxyUrl").value.trim()) {
      highlightFieldError("proxy_url", "基础设定 → 代理地址：启用代理后必须填写代理地址 (如 http://127.0.0.1:7890)");
      return false;
    }
    var dlTimeout = parseInt(el("cfgDownloadTimeout").value, 10);
    if (isNaN(dlTimeout) || dlTimeout < 1) {
      highlightFieldError("download_timeout", "基础设定 → 下载超时：必须填写大于等于 1 的秒数");
      return false;
    }
    var apiTimeout = parseInt(el("cfgApiTimeout").value, 10);
    if (isNaN(apiTimeout) || apiTimeout < 10) {
      highlightFieldError("api_timeout", "基础设定 → API 超时：必须填写大于等于 10 的秒数");
      return false;
    }

    // 2. 参数设定校验
    var openAiTemp = el("cfgOpenaiTemp").value.trim();
    if (openAiTemp !== "") {
      var ot = parseFloat(openAiTemp);
      if (isNaN(ot) || ot < 0 || ot > 2) {
        highlightFieldError("openai_temperature", "参数设定 → OpenAI 温度：必须在 0.0 到 2.0 之间");
        return false;
      }
    }
    var openAiMaxTokens = el("cfgOpenaiMaxTokens").value.trim();
    if (openAiMaxTokens !== "") {
      var omt = parseInt(openAiMaxTokens, 10);
      if (isNaN(omt) || omt < 64 || omt > 16384) {
        highlightFieldError("openai_max_tokens", "参数设定 → OpenAI Max Tokens：必须在 64 到 16384 之间");
        return false;
      }
    }

    var novalaiSteps = el("cfgNovalaiSteps").value.trim();
    if (novalaiSteps !== "") {
      var ns = parseInt(novalaiSteps, 10);
      if (isNaN(ns) || ns < 1 || ns > 50) {
        highlightFieldError("novalai_steps", "参数设定 → NovelAI 采样步数：必须在 1 到 50 之间");
        return false;
      }
    }
    var novalaiScale = el("cfgNovalaiScale").value.trim();
    if (novalaiScale !== "") {
      var nsc = parseFloat(novalaiScale);
      if (isNaN(nsc) || nsc < 0 || nsc > 10) {
        highlightFieldError("novalai_scale", "参数设定 → NovelAI 引导强度：必须在 0.0 到 10.0 之间");
        return false;
      }
    }

    // 3. 增强模块校验
    var taggerTemp = el("cfgTaggerTemp").value.trim();
    if (taggerTemp !== "") {
      var tt = parseFloat(taggerTemp);
      if (isNaN(tt) || tt < 0 || tt > 2) {
        highlightFieldError("tagger_temperature", "增强模块 → 打标温度：必须在 0.0 到 2.0 之间");
        return false;
      }
    }

    // 4. 次数与签到
    if (el("cfgEnableCheckin").checked && el("cfgRandomCheckin").checked) {
      var cMin = parseInt(el("cfgCheckinMin").value, 10) || 0;
      var cMax = parseInt(el("cfgCheckinMax").value, 10) || 0;
      if (cMin > cMax) {
        highlightFieldError("checkin_random_reward_min", "基础设定 → 签到系统：随机奖励下限不能大于上限");
        return false;
      }
    }

    return true;
  }

  async function saveConfig() {
    if (!validateConfigBeforeSave()) return;

    var body = {
      /* 基础 */
      prefix: el("cfgPrefix").checked,
      extra_prefix: el("cfgExtraPrefix").value.trim(),
      use_proxy: el("cfgUseProxy").checked,
      proxy_url: el("cfgProxyUrl").value.trim(),
      download_timeout: parseInt(el("cfgDownloadTimeout").value, 10) || 60,
      download_retries: parseInt(el("cfgDownloadRetries").value, 10) || 0,
      api_timeout: parseInt(el("cfgApiTimeout").value, 10) || 120,
      /* OpenAI */
      openai_size: el("cfgOpenaiSize").value,
      openai_temperature: parseFloat(el("cfgOpenaiTemp").value) || 1.0,
      openai_max_tokens: parseInt(el("cfgOpenaiMaxTokens").value, 10) || 2048,
      /* Gemini */
      safety_level: el("cfgSafety").value,
      image_size: el("cfgImageSize").value,
      hires_whitelist: splitLines(el("cfgHires").value),
      /* NovalAI */
      novalai_size: el("cfgNovalaiSize").value,
      novalai_sampler: el("cfgNovalaiSampler").value,
      novalai_steps: parseInt(el("cfgNovalaiSteps").value, 10) || 23,
      novalai_scale: parseFloat(el("cfgNovalaiScale").value) || 6.0,
      novalai_negative_prompt: el("cfgNovalaiNeg").value,
      /* NovelAI 预检 */
      novai_precheck_enabled: el("cfgNovaiPrecheck").checked,
      novai_precheck_timeout: parseInt(el("cfgNovaiPrecheckTimeout").value, 10) || 10,
      novai_point_warn_threshold: parseInt(el("cfgNovaiWarn").value, 10) || 0,
      /* 自动撤回 */
      auto_recall_enabled: el("cfgAutoRecall").checked,
      auto_recall_delay: parseInt(el("cfgAutoRecallDelay").value, 10) || 0,
      auto_recall_allowed_groups: splitLines(el("cfgAutoRecallGroups").value),
      /* NSFW 过滤 */
      nsfw_filter_enabled: el("cfgNsfwFilter").checked,
      nsfw_filter_tags: el("cfgNsfwTags").value,
      nsfw_zip_enabled: el("cfgNsfwZipEnabled").checked,
      /* 打标 */
      tagger_provider_id: el("cfgTaggerProvider").value.trim(),
      tagger_temperature: parseFloat(el("cfgTaggerTemp").value) || 0.2,
      tagger_max_tokens: parseInt(el("cfgTaggerMaxTokens").value, 10) || 900,
      /* 画师推荐 */
      artist_recommend_enabled: el("cfgArtistRecEnabled").checked,
      artist_recommend_max_candidates: parseInt(el("cfgArtistRecMax").value, 10) || 0,
      artist_recommend_min_match: parseInt(el("cfgArtistRecMin").value, 10) || 0,
      /* 自拍特征 */
      selfie_prompt: el("cfgSelfiePrompt").value,
      /* 权限 */
      user_whitelist: splitLines(el("cfgUserWhite").value),
      user_blacklist: splitLines(el("cfgUserBlack").value),
      group_whitelist: splitLines(el("cfgGroupWhite").value),
      group_blacklist: splitLines(el("cfgGroupBlack").value),
      /* 次数限制 */
      enable_user_limit: el("cfgEnableUser").checked,
      enable_group_limit: el("cfgEnableGroup").checked,
      user_count_cap: parseInt(el("cfgUserCap").value, 10) || 0,
      group_task_limit: parseInt(el("cfgTaskLimit").value, 10) || 0,
      /* 签到系统 */
      enable_checkin: el("cfgEnableCheckin").checked,
      checkin_fixed_reward: parseInt(el("cfgCheckinFixed").value, 10) || 0,
      enable_random_checkin: el("cfgRandomCheckin").checked,
      checkin_random_reward_min: parseInt(el("cfgCheckinMin").value, 10) || 0,
      checkin_random_reward_max: parseInt(el("cfgCheckinMax").value, 10) || 0,
      /* 帮助菜单 */
      help_menu: el("cfgHelpMenu").value,
    };
    var btn = el("btnConfigSave");
    btn.disabled = true;
    try {
      await apiPost("config", body);
      clearFieldErrors();
      toast("设置已保存", "success");
    } catch (e) {
      // 解析后端友好的错误提示，匹配对应字段进行定位高亮
      var errMsg = e.message || "未知错误";
      var matchedField = null;
      for (var fKey in FIELD_ELEMENT_MAP) {
        if (errMsg.indexOf(fKey) >= 0) {
          matchedField = fKey;
          break;
        }
      }
      if (matchedField) {
        highlightFieldError(matchedField, "保存失败：" + errMsg);
      } else {
        toast("保存失败：" + errMsg, "error");
      }
    } finally {
      btn.disabled = false;
    }
  }

  // 单独保存绘图 Prompt 预设
  async function savePromptPresets() {
    clearFieldErrors();
    var parsed = parsePromptList(el("cfgPromptList").value);
    var btn = el("btnPromptPresetSave");
    btn.disabled = true;
    try {
      await apiPost("config", { prompt_list: parsed });
      toast("绘图 Prompt 预设已保存（" + parsed.length + " 条）", "success");
    } catch (e) {
      var errMsg = e.message || "未知错误";
      highlightFieldError("prompt_list", "保存失败：" + errMsg);
    } finally {
      btn.disabled = false;
    }
  }

  /* ============================================================
   * 导航 / 事件绑定
   * ============================================================ */
  var PAGE_TITLES = { models: "提供商管理", workbench: "生图测试台", counts: "次数管理", logs: "生图日志", artists: "预设", settings: "设置" };
  var CURRENT = "models";

  async function switchPage(name) {
    if (CURRENT === "models" && providerView === "detail" && isProviderDirty && name !== "models") {
      if (!(await confirmDialog("当前提供商有未保存的修改，确定离开吗？未保存的内容将丢失。", "未保存提示"))) return;
      isProviderDirty = false;
    }
    CURRENT = name;
    document.querySelectorAll(".nav-item").forEach(function (b) {
      var active = b.getAttribute("data-nav") === name;
      b.classList.toggle("is-active", active);
      if (active) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    document.querySelectorAll(".page").forEach(function (p) {
      p.hidden = p.getAttribute("data-page") !== name;
    });
    el("pageTitle").textContent = PAGE_TITLES[name] || name;
  }

  function refreshCurrent() {
    if (CURRENT === "models") {
      if (providerView === "detail" && currentProviderId != null) {
        openProvider(currentProviderId);
      } else {
        loadProviders();
      }
    }
    else if (CURRENT === "workbench") loadWorkbenchData();
    else if (CURRENT === "counts") loadCounts(true);
    else if (CURRENT === "logs") loadLogs(true);
    else if (CURRENT === "artists") {
      loadArtistPresets();
      loadArtistGroups();
      loadConfig(); // 同时加载 prompt_list
    }
    else if (CURRENT === "settings") loadConfig();
  }

  function bindEvents() {
    /* 导航 */
    document.querySelectorAll(".nav-item").forEach(function (b) {
      b.addEventListener("click", function () {
        switchPage(b.getAttribute("data-nav"));
        // 切页后立即触发对应页加载（修复首次打开一直「加载中」直到手动刷新的问题）
        refreshCurrent();
      });
    });
    el("btnRefresh").addEventListener("click", refreshCurrent);

    /* 提供商 */
    el("btnAddProvider").addEventListener("click", showAddProvider);
    el("btnBackProviders").addEventListener("click", backToProviders);
    el("btnDeleteProvider").addEventListener("click", function () {
      if (currentProviderId != null) deleteProvider(currentProviderId);
    });
    if (el("btnProviderSaveAll")) el("btnProviderSaveAll").addEventListener("click", saveEntireProvider);
    el("btnProviderCfgSave").addEventListener("click", saveEntireProvider);
    el("providersList").addEventListener("click", function (e) {
      var card = e.target.closest(".provider-card");
      if (!card) return;
      var id = card.getAttribute("data-id");
      var actBtn = e.target.closest("[data-act]");
      if (actBtn) {
        var act = actBtn.getAttribute("data-act");
        if (act === "models" || act === "edit") openProvider(id);
        else if (act === "del") deleteProvider(id);
        return; // toggle 开关走 change 事件，不进入详情
      }
      // 点卡片空白处也进入详情
      openProvider(id);
    });
    el("providersList").addEventListener("change", function (e) {
      var sw = e.target.closest('[data-act="toggle"]');
      if (!sw) return;
      var card = e.target.closest(".provider-card");
      if (!card) return;
      var id = card.getAttribute("data-id");
      var p = null;
      for (var i = 0; i < providersData.length; i++) {
        if (String(providersData[i].id) === String(id)) { p = providersData[i]; break; }
      }
      if (p) toggleProvider(p, sw.checked);
    });

    /* 模型（详情视图内，按当前提供商） */
    el("btnModelsAdd").addEventListener("click", function () {
      syncModelsDataFromDOM();
      modelsData.push({ name: "", quota: 1, desc: "", params: {}, allowed_sizes: null });
      renderModels();
      markProviderDirty();
      var last = el("modelsBody").querySelector('tr[data-i="' + (modelsData.length - 1) + '"]');
      if (last) { var nm = last.querySelector('[data-k="name"]'); if (nm) nm.focus(); }
    });
    el("btnModelsSave").addEventListener("click", saveEntireProvider);
    el("btnModelsReset").addEventListener("click", function () {
      if (currentProviderId == null) return;
      loadProviderModels(currentProviderId);
      isProviderDirty = false;
      toast("已重置为该提供商当前模型", "info");
    });
    el("modelsBody").addEventListener("click", async function (e) {
      var btn = e.target.closest("[data-act='del']");
      if (!btn) return;
      var tr = btn.closest("tr[data-i]");
      if (!tr) return;
      var i = parseInt(tr.getAttribute("data-i"), 10);
      var name = (modelsData[i] && modelsData[i].name) || "";
      if (name && !(await confirmDialog("确认删除模型「" + name + "」？（保存后生效）", "删除模型"))) return;
      syncModelsDataFromDOM();
      modelsData.splice(i, 1);
      renderModels();
      markProviderDirty();
    });
    el("modelsBody").addEventListener("input", markProviderDirty);
    el("modelsBody").addEventListener("change", markProviderDirty);
    ["pdName", "pdApiKey", "pdBaseUrl", "pdUserUrl"].forEach(function (id) {
      var elem = el(id);
      if (elem) elem.addEventListener("input", markProviderDirty);
    });
    ["pdEnabled", "pdReverseProxy"].forEach(function (id) {
      var elem = el(id);
      if (elem) elem.addEventListener("change", markProviderDirty);
    });

    /* 添加/编辑提供商弹层 */
    el("providerClose").addEventListener("click", function () { closeModal("providerModal"); });
    el("providerCancel").addEventListener("click", function () { closeModal("providerModal"); });
    el("providerSave").addEventListener("click", saveProviderFromModal);
    el("templateRow").addEventListener("click", function (e) {
      var c = e.target.closest(".template-card");
      if (!c) return;
      selectTemplate(c.getAttribute("data-type"));
    });

    /* 次数 */
    el("countSeg").addEventListener("click", function (e) {
      var item = e.target.closest(".seg-item");
      if (!item) return;
      countKind = item.getAttribute("data-count-kind");
      el("countSeg").querySelectorAll(".seg-item").forEach(function (s) {
        var active = s === item;
        s.classList.toggle("is-active", active);
        s.setAttribute("aria-selected", active ? "true" : "false");
      });
      countSearch = "";
      el("countSearch").value = "";
      loadCounts(true);
    });
    el("countSearch").addEventListener("input", debounce(function () {
      countSearch = el("countSearch").value.trim();
      loadCounts(true);
    }, 300));
    el("countsBody").addEventListener("click", function (e) {
      var setBtn = e.target.closest("[data-act='set']");
      if (setBtn) {
        openSet(setBtn.getAttribute("data-id"), setBtn.getAttribute("data-id"),
                parseInt(setBtn.getAttribute("data-count"), 10));
        return;
      }
      var btn = e.target.closest("[data-act='recharge']");
      if (!btn) return;
      openRecharge(btn.getAttribute("data-id"), btn.getAttribute("data-id"));
    });
    el("countPrev").addEventListener("click", function () { if (countPage > 1) { countPage--; loadCounts(false); } });
    el("countNext").addEventListener("click", function () {
      var totalPages = Math.max(1, Math.ceil(countTotal / PAGE_SIZE));
      if (countPage < totalPages) { countPage++; loadCounts(false); }
    });

    /* 充值弹层 */
    el("rechargeClose").addEventListener("click", function () { closeModal("rechargeModal"); });
    el("rechargeCancel").addEventListener("click", function () { closeModal("rechargeModal"); });
    el("rechargeConfirm").addEventListener("click", confirmRecharge);
    el("rechargeChips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      el("rechargeChips").querySelectorAll(".chip").forEach(function (c) { c.classList.remove("is-selected"); });
      chip.classList.add("is-selected");
      el("rechargeAmount").value = chip.getAttribute("data-amt");
    });

    /* 通用确认弹层 */
    el("confirmOk").addEventListener("click", function () { confirmFinish(true); });
    el("confirmCancel").addEventListener("click", function () { confirmFinish(false); });
    el("confirmClose").addEventListener("click", function () { confirmFinish(false); });

    /* 日志 */
    el("logScope").addEventListener("change", function () { loadLogs(true); });
    el("logDays").addEventListener("change", function () { loadLogs(true); });
    el("logGroup").addEventListener("change", function () { loadLogs(true); });
    el("logsMore").addEventListener("click", function () { logPage++; loadLogs(false); });
    el("logsBody").addEventListener("click", async function (e) {
      var card = e.target.closest(".log-card");
      if (!card) return;
      var ts = card.getAttribute("data-ts");
      if (e.target.closest('[data-act="load-wb"]')) loadLogToWorkbench(ts);
      else if (e.target.closest('[data-act="view"]')) viewLogDetail(ts);
      else if (e.target.closest('[data-act="del"]')) {
        if (await confirmDialog("确认删除该条生图记录？", "删除记录")) deleteLog(ts);
      }
    });

    /* 查看大图弹层 */
    el("viewClose").addEventListener("click", function () { closeModal("viewModal"); });
    el("viewCloseBtn").addEventListener("click", function () { closeModal("viewModal"); });

    /* 设置 */
    el("btnConfigSave").addEventListener("click", saveConfig);
    el("btnPromptPresetSave").addEventListener("click", savePromptPresets);

    /* 设置页面顶部分栏 Tab 切换 */
    document.querySelectorAll(".sub-tab-btn[data-cfgtab]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchConfigTab(btn.getAttribute("data-cfgtab"));
      });
    });

    /* 预设页面子 Tab 切换 */
    document.querySelectorAll(".sub-tab-btn[data-subtab]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchPresetSubTab(btn.getAttribute("data-subtab"));
      });
    });

    /* 画师预设导入 / 导出 / 夺舍 */
    el("btnArtistExport").addEventListener("click", exportArtistPresets);
    el("btnArtistImport").addEventListener("click", openArtistImportModal);
    el("artistImportClose").addEventListener("click", function () { closeModal("artistImportModal"); });
    el("artistImportCancel").addEventListener("click", function () { closeModal("artistImportModal"); });
    el("artistImportConfirm").addEventListener("click", doImportArtistPresets);
    el("btnNaiBrushTakeover").addEventListener("click", takeoverNaiBrush);
    el("btnChooseImportFile").addEventListener("click", function () {
      el("artistImportFileInput").click();
    });
    el("artistImportFileInput").addEventListener("change", function (e) {
      var file = e.target.files && e.target.files[0];
      if (!file) return;
      el("importFileTip").textContent = file.name;
      var reader = new FileReader();
      reader.onload = function (evt) {
        el("artistImportText").value = evt.target.result;
      };
      reader.readAsText(file, "UTF-8");
    });

    /* 生图测试台 (Workbench Studio) */
    el("wbModel").addEventListener("change", function () {
      onWbModelChange(this.value);
    });
    el("wbPresetSelect").addEventListener("change", function () {
      var pName = this.value;
      if (!pName) return;
      var target = artistPresetsData.find(function (p) { return p.name === pName; });
      if (target) {
        var curPrompt = el("wbPrompt").value.trim();
        var addText = target.artists;
        if (curPrompt) {
          el("wbPrompt").value = curPrompt + ", " + addText;
        } else {
          el("wbPrompt").value = addText;
        }
        if (target.negative) {
          var curNeg = el("wbNegative").value.trim();
          if (curNeg) {
            el("wbNegative").value = curNeg + ", " + target.negative;
          } else {
            el("wbNegative").value = target.negative;
          }
        }
        toast("已追加预设「" + pName + "」的画师串与负面词", "info");
      }
      this.value = "";
    });

    // 滑块联动展示
    el("wbNaiSteps").addEventListener("input", function () { el("wbNaiStepsVal").textContent = this.value; });
    el("wbNaiScale").addEventListener("input", function () { el("wbNaiScaleVal").textContent = parseFloat(this.value).toFixed(1); });
    el("wbGeminiTemp").addEventListener("input", function () { el("wbGeminiTempVal").textContent = parseFloat(this.value).toFixed(2); });
    el("wbOpenaiTemp").addEventListener("input", function () { el("wbOpenaiTempVal").textContent = parseFloat(this.value).toFixed(2); });

    // 参考图选择与上传
    el("wbRefDropzone").addEventListener("click", function () { el("wbRefFileInput").click(); });
    el("wbRefFileInput").addEventListener("change", function (e) {
      var file = e.target.files && e.target.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function (evt) {
        wbRefImageBase64 = evt.target.result;
        el("wbRefImg").src = wbRefImageBase64;
        el("wbRefFileName").textContent = file.name;
        el("wbRefFileSize").textContent = Math.round(file.size / 1024) + " KB";
        el("wbRefEmptyView").hidden = true;
        el("wbRefPreviewView").hidden = false;
        el("btnWbClearRef").hidden = false;
        toast("参考图片已载入（图生图模式）", "info");
      };
      reader.readAsDataURL(file);
    });
    el("btnWbClearRef").addEventListener("click", function (e) {
      e.stopPropagation();
      clearWbRefImage();
      toast("已清除参考图", "info");
    });

    // 生成与重置
    el("btnWbGenerate").addEventListener("click", doWorkbenchGenerate);
    el("btnWbReset").addEventListener("click", resetWbForm);
    el("btnWbDownload").addEventListener("click", downloadWbImage);
    el("btnWbViewLarge").addEventListener("click", function () {
      if (!wbLastResult || !wbLastResult.image_base64) return;
      openModal("viewModal");
      el("viewImageWrap").hidden = false;
      el("viewImage").src = wbLastResult.image_base64;
      el("viewPlaceholder").hidden = true;
      el("viewDetail").innerHTML =
        '<div class="dl-item"><span class="dl-k">模型</span><span class="dl-v">' + esc(wbLastResult.model || "-") + "</span></div>" +
        '<div class="dl-item"><span class="dl-k">耗时</span><span class="dl-v">' + esc(wbLastResult.elapsed + "s") + "</span></div>" +
        '<div class="dl-item"><span class="dl-k">提示词</span><span class="dl-v">' + esc(el("wbPrompt").value) + "</span></div>";
    });

    /* 画师预设 */
    el("btnArtistPresetsAdd").addEventListener("click", function () {
      artistPresetsData.push({ name: "", artists: "", negative: "" });
      renderArtistPresets();
      var last = el("artistPresetsBody").querySelector('tr[data-i="' + (artistPresetsData.length - 1) + '"]');
      if (last) { var nm = last.querySelector('[data-k="name"]'); if (nm) nm.focus(); }
    });
    el("btnArtistPresetsSave").addEventListener("click", saveArtistPresets);
    el("artistPresetsBody").addEventListener("click", async function (e) {
      var btn = e.target.closest("[data-act='del']");
      if (!btn) return;
      var tr = btn.closest("tr[data-i]");
      if (!tr) return;
      var i = parseInt(tr.getAttribute("data-i"), 10);
      var name = artistPresetsData[i] && artistPresetsData[i].name;
      if (name && !(await confirmDialog("确认删除预设「" + name + "」？（未保存前不生效）", "删除预设"))) return;
      artistPresetsData.splice(i, 1);
      renderArtistPresets();
    });
    el("btnArtistGroupSave").addEventListener("click", function () {
      saveArtistGroup(el("artistGroupPreset").value);
    });
    el("btnArtistGroupOff").addEventListener("click", function () {
      var gid = el("artistGroupId").value.trim();
      if (!gid) { toast("请填写群 ID", "error"); return; }
      saveArtistGroup("");
    });
    el("artistGroupMap").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act='off']");
      if (!btn) return;
      var item = btn.closest(".group-map-item");
      if (!item) return;
      el("artistGroupId").value = item.getAttribute("data-gid");
      saveArtistGroup("");
    });

    /* Esc 关闭弹层 / 遮罩点击关闭 */
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (!el("confirmModal").hidden) confirmFinish(false);
        if (!el("providerModal").hidden) closeModal("providerModal");
        if (!el("rechargeModal").hidden) closeModal("rechargeModal");
        if (!el("viewModal").hidden) closeModal("viewModal");
      }
    });
    // 提供商表单可能包含未保存的 API Key 和模型配置，点击遮罩时不自动关闭。
    ["rechargeModal", "viewModal"].forEach(function (id) {
      el(id).addEventListener("click", function (e) {
        if (e.target === el(id)) closeModal(id);
      });
    });
    el("confirmModal").addEventListener("click", function (e) {
      if (e.target === el("confirmModal")) confirmFinish(false);
    });
  }

  /* ============================================================
   * 启动
   * ============================================================ */
  async function init() {
    bindEvents();
    // 兜底：若 bridge 尚未注入（显式引入 bridge-sdk 后正常应已就绪），短时轮询等待
    for (var i = 0; i < 50 && !(bridge && bridge.ready); i++) {
      bridge = window.AstrBotPluginPage;
      await new Promise(function (r) { setTimeout(r, 100); });
    }
    if (bridge && bridge.ready) {
      try {
        ctx = await bridge.ready();
        if (ctx && typeof ctx.isDark === "boolean") {
          document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
        }
        if (bridge.onContext) bridge.onContext(function (c) {
          if (c && typeof c.isDark === "boolean") {
            document.documentElement.setAttribute("data-theme", c.isDark ? "dark" : "light");
          }
        });
      } catch (e) {
        toast("Bridge 初始化失败：" + e.message, "error");
      }
    } else {
      // bridge 未注入/未就绪：给出明确提示（需在 Dashboard 登录后打开本页）
      toast("请在 Dashboard 登录后打开本页", "error");
      switchPage("models");
      el("providersList").innerHTML =
        '<div class="empty"><span class="empty-main">Bridge 未就绪，请在 Dashboard 登录后打开本页</span></div>';
      return;
    }
    switchPage("models");
    loadProviders();
    loadGroupOptions();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
