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
   * Tab 1 · 模型管理
   * ============================================================ */
  var modelsData = []; // [{name, quota, desc, params:{}, allowed_sizes:null|[]}]
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

  async function loadModels() {
    try {
      var data = await apiGet("models");
      modelsData = (data && data.models) || [];
      renderModels();
      el("modelsNote").textContent = "已加载 " + modelsData.length + " 个模型";
    } catch (e) {
      toast("加载模型失败：" + e.message, "error");
      el("modelsBody").innerHTML =
        '<tr class="row-empty"><td colspan="6"><div class="empty"><span class="empty-main">加载失败：' + esc(e.message) + "</span></div></td></tr>";
    }
  }

  async function saveModels() {
    var r = collectModels();
    if (!r.ok) { toast("模型信息不合法：请检查模型名非空、参数 JSON 合法", "error"); return; }
    var btn = el("btnModelsSave");
    btn.disabled = true;
    try {
      var res = await apiPost("models", { models: r.models });
      modelsData = r.models;
      renderModels();
      toast("已保存 " + (res && res.count != null ? res.count : r.models.length) + " 个模型", "success");
    } catch (e) {
      toast("保存失败：" + e.message, "error");
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
          '<td><button type="button" class="btn btn-ghost btn-sm" data-act="recharge" data-id="' + esc(id) + '">充值</button></td>' +
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
    rechargeTarget = { kind: countKind, id: id, label: label };
    el("rechargeDesc").textContent = "目标：" + (countKind === "users" ? "用户 " : "群 ") + label;
    el("rechargeAmount").value = "";
    el("rechargeAmount").classList.remove("invalid");
    el("rechargeChips").querySelectorAll(".chip").forEach(function (c) { c.classList.remove("is-selected"); });
    openModal("rechargeModal");
    setTimeout(function () { el("rechargeAmount").focus(); }, 30);
  }

  async function confirmRecharge() {
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
      toast("当前充值接口仅支持正整数（扣减需另设配额）", "error");
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
     无 IntersectionObserver 时降级为只加载前 5 条。 */
  var thumbObserver = null;
  function scheduleThumbLoad() {
    var thumbs = Array.prototype.slice.call(el("logsBody").querySelectorAll(".thumb[data-path]"));
    if (!thumbs.length) return;
    if (!("IntersectionObserver" in window)) {
      thumbs.slice(0, 5).forEach(loadThumb);
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
        thumbEl.innerHTML = '<img class="thumb-img" src="' + res.image + '" alt="生图缩略图">';
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
  var PAGE_TITLES = { models: "模型管理", counts: "次数管理", logs: "生图日志", settings: "基础设置" };
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
    if (CURRENT === "models") loadModels();
    else if (CURRENT === "counts") loadCounts(true);
    else if (CURRENT === "logs") loadLogs(true);
    else if (CURRENT === "settings") loadConfig();
  }

  function bindEvents() {
    /* 导航 */
    document.querySelectorAll(".nav-item").forEach(function (b) {
      b.addEventListener("click", function () { switchPage(b.getAttribute("data-nav")); });
    });
    el("btnRefresh").addEventListener("click", refreshCurrent);

    /* 模型 */
    el("btnModelsAdd").addEventListener("click", function () {
      modelsData.push({ name: "", quota: 1, desc: "", params: {}, allowed_sizes: null });
      renderModels();
      var last = el("modelsBody").querySelector('tr[data-i="' + (modelsData.length - 1) + '"]');
      if (last) { var nm = last.querySelector('[data-k="name"]'); if (nm) nm.focus(); }
    });
    el("btnModelsSave").addEventListener("click", saveModels);
    el("btnModelsReset").addEventListener("click", function () {
      loadModels();
      toast("已重置为后端当前模型", "info");
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
        if (!el("rechargeModal").hidden) closeModal("rechargeModal");
        if (!el("viewModal").hidden) closeModal("viewModal");
      }
    });
    ["rechargeModal", "viewModal"].forEach(function (id) {
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
    }
    switchPage("models");
    loadModels();
    loadGroupOptions();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
