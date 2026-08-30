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

  /* ---------- bridge 封装 ---------- */
  async function apiGet(endpoint, params) { return await bridge.apiGet(endpoint, params || {}); }
  async function apiPost(endpoint, body) { return await bridge.apiPost(endpoint, body || {}); }

  /* ============================================================
   * Tab 1 · 提供商管理
   * ============================================================ */
  var providersData = [];    // [{id, name, type, api_key_masked, base_url, enabled, model_count}]
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
    novalai: { base_url: "https://api.novalai.io/v1" },
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

  function collectModels() {
    var rows = el("modelsBody").querySelectorAll("tr[data-i]");
    var out = [];
    var seen = {};
    var ok = true;
    rows.forEach(function (tr) {
      var m = { name: "", quota: 1, desc: "", params: {}, allowed_sizes: null };
      tr.querySelectorAll("[data-k]").forEach(function (inp) {
        var k = inp.getAttribute("data-k");
        var v = inp.value.trim();
        if (k === "name") m.name = v;
        else if (k === "quota") m.quota = parseInt(v, 10);
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
      if (!m.name) {
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

  async function saveProviderModels() {
    if (currentProviderId == null) return;
    var r = collectModels();
    if (!r.ok) { toast("模型信息不合法：请检查模型名非空、参数 JSON 合法", "error"); return; }
    var btn = el("btnModelsSave");
    btn.disabled = true;
    try {
      var res = await apiPost("providers/models", { provider: currentProviderId, models: r.models });
      modelsData = r.models;
      renderModels();
      toast("已保存 " + (res && res.count != null ? res.count : r.models.length) + " 个模型", "success");
    } catch (e) {
      toast("保存失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
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
        '<span class="pm-item"><span class="pm-k">API Key</span><code>' + esc(p.api_key_masked || maskKey("")) + "</code></span>" +
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
    el("pdEnabled").checked = !!p.enabled;
    showDetailView();
    await loadProviderModels(id);
  }

  function backToProviders() {
    currentProviderId = null;
    showProvidersView();
    loadProviders(); // 刷新模型数等
  }

  async function saveProviderConfig() {
    if (currentProviderId == null) return;
    var name = el("pdName").value.trim();
    if (!name) {
      el("pdName").classList.add("invalid");
      toast("请填写提供商名字", "error");
      return;
    }
    var key = el("pdApiKey").value.trim();
    var url = el("pdBaseUrl").value.trim();
    var btn = el("btnProviderCfgSave");
    btn.disabled = true;
    try {
      await apiPost("providers/save", {
        id: currentProviderId, name: name, api_key: key, base_url: url,
        enabled: el("pdEnabled").checked,
      });
      el("pdApiKey").value = ""; // 已提交，清空掩码输入
      el("pdTitle").textContent = name;
      toast("提供商配置已保存", "success");
      await loadProviders(); // 同步列表数据
    } catch (e) {
      toast("保存失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function toggleProvider(p, next) {
    var prev = !!p.enabled;
    p.enabled = next;
    renderProviders();
    try {
      await apiPost("providers/save", {
        id: p.id, name: p.name || "", api_key: "", base_url: p.base_url || "",
        enabled: next,
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
    if (!confirm("确认删除提供商「" + p.name + "」？其下所有模型配置将一并删除。")) return;
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
    el("provName").classList.remove("invalid");
    el("provKey").classList.remove("invalid");
    selectTemplate("gemini");
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
    el("provEnabled").checked = !!p.enabled;
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
    // 预填默认 BaseURL（仅当字段为空，避免覆盖用户输入）
    var tpl = PROVIDER_TEMPLATES[type];
    if (tpl && tpl.base_url && !el("provUrl").value.trim()) {
      el("provUrl").value = tpl.base_url;
    }
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
    var btn = el("providerSave");
    btn.disabled = true;
    try {
      if (providerModalMode === "edit") {
        await apiPost("providers/save", {
          id: editProviderId, name: name, api_key: key, base_url: url,
          enabled: el("provEnabled").checked,
        });
        toast("提供商「" + name + "」已更新", "success");
      } else {
        await apiPost("providers", {
          name: name, type: selectedTemplateType, api_key: key, base_url: url,
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
   * Tab 4 · 基础设置
   * ============================================================ */
  async function loadConfig() {
    try {
      var c = await apiGet("config");
      el("cfgApiUrl").value = c.api_url || "";
      el("cfgApiKeys").value = (c.api_keys && c.api_keys.join("\n")) || "";
      el("cfgIsGemini").checked = !!c.is_gemini_api;
      el("cfgSafety").value = c.safety_level || "";
      el("cfgImageSize").value = (c.image_size || "").toUpperCase();
      el("cfgHires").value = (c.hires_whitelist || []).join("\n");
      el("cfgEnableUser").checked = c.enable_user_limit !== false;
      el("cfgEnableGroup").checked = !!c.enable_group_limit;
      el("cfgUserCap").value = c.user_count_cap || 0;
      el("cfgTaskLimit").value = c.group_task_limit != null ? c.group_task_limit : 2;
      el("cfgEnableCheckin").checked = !!c.enable_checkin;
      el("cfgUserWhite").value = (c.user_whitelist || []).join("\n");
      el("cfgUserBlack").value = (c.blacklist || []).join("\n");
    } catch (e) {
      toast("加载配置失败：" + e.message, "error");
    }
  }

  function splitLines(v) {
    return String(v || "").split(/\r?\n/).map(function (s) { return s.trim(); }).filter(function (s) { return s; });
  }

  async function saveConfig() {
    var body = {
      api_url: el("cfgApiUrl").value.trim(),
      is_gemini_api: el("cfgIsGemini").checked,
      safety_level: el("cfgSafety").value,
      image_size: el("cfgImageSize").value,
      hires_whitelist: splitLines(el("cfgHires").value),
      enable_user_limit: el("cfgEnableUser").checked,
      enable_group_limit: el("cfgEnableGroup").checked,
      user_count_cap: parseInt(el("cfgUserCap").value, 10) || 0,
      group_task_limit: parseInt(el("cfgTaskLimit").value, 10) || 0,
      enable_checkin: el("cfgEnableCheckin").checked,
      user_whitelist: splitLines(el("cfgUserWhite").value),
      blacklist: splitLines(el("cfgUserBlack").value),
    };
    var btn = el("btnConfigSave");
    btn.disabled = true;
    try {
      await apiPost("config", body);
      toast("设置已保存", "success");
    } catch (e) {
      toast("保存失败：" + e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /* ============================================================
   * 导航 / 事件绑定
   * ============================================================ */
  var PAGE_TITLES = { models: "提供商管理", counts: "次数管理", logs: "生图日志", settings: "基础设置" };
  var CURRENT = "models";

  function switchPage(name) {
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
    else if (CURRENT === "counts") loadCounts(true);
    else if (CURRENT === "logs") loadLogs(true);
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
    el("btnProviderCfgSave").addEventListener("click", saveProviderConfig);
    el("providersList").addEventListener("click", function (e) {
      var card = e.target.closest(".provider-card");
      if (!card) return;
      var id = card.getAttribute("data-id");
      var actBtn = e.target.closest("[data-act]");
      if (actBtn) {
        var act = actBtn.getAttribute("data-act");
        if (act === "models") openProvider(id);
        else if (act === "edit") showEditProvider(id);
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
      modelsData.push({ name: "", quota: 1, desc: "", params: {}, allowed_sizes: null });
      renderModels();
      var last = el("modelsBody").querySelector('tr[data-i="' + (modelsData.length - 1) + '"]');
      if (last) { var nm = last.querySelector('[data-k="name"]'); if (nm) nm.focus(); }
    });
    el("btnModelsSave").addEventListener("click", saveProviderModels);
    el("btnModelsReset").addEventListener("click", function () {
      if (currentProviderId == null) return;
      loadProviderModels(currentProviderId);
      toast("已重置为该提供商当前模型", "info");
    });
    el("modelsBody").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act='del']");
      if (!btn) return;
      var tr = btn.closest("tr[data-i]");
      if (!tr) return;
      var i = parseInt(tr.getAttribute("data-i"), 10);
      var name = modelsData[i] && modelsData[i].name;
      if (name && !confirm("确认删除模型「" + name + "」？（未保存前不生效）")) return;
      modelsData.splice(i, 1);
      renderModels();
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

    /* 日志 */
    el("logScope").addEventListener("change", function () { loadLogs(true); });
    el("logDays").addEventListener("change", function () { loadLogs(true); });
    el("logGroup").addEventListener("change", function () { loadLogs(true); });
    el("logsMore").addEventListener("click", function () { logPage++; loadLogs(false); });
    el("logsBody").addEventListener("click", function (e) {
      var card = e.target.closest(".log-card");
      if (!card) return;
      var ts = card.getAttribute("data-ts");
      if (e.target.closest('[data-act="view"]')) viewLogDetail(ts);
      else if (e.target.closest('[data-act="del"]')) {
        if (confirm("确认删除该条生图记录？")) deleteLog(ts);
      }
    });

    /* 查看大图弹层 */
    el("viewClose").addEventListener("click", function () { closeModal("viewModal"); });
    el("viewCloseBtn").addEventListener("click", function () { closeModal("viewModal"); });

    /* 设置 */
    el("btnConfigSave").addEventListener("click", saveConfig);

    /* Esc 关闭弹层 / 遮罩点击关闭 */
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (!el("providerModal").hidden) closeModal("providerModal");
        if (!el("rechargeModal").hidden) closeModal("rechargeModal");
        if (!el("viewModal").hidden) closeModal("viewModal");
      }
    });
    ["providerModal", "rechargeModal", "viewModal"].forEach(function (id) {
      el(id).addEventListener("click", function (e) {
        if (e.target === el(id)) closeModal(id);
      });
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
