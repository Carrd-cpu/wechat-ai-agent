/* 企业微信 AI 智能客服助手（模拟版）前端逻辑 */
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    customerList: $("customerList"),
    messages: $("messages"),
    input: $("input"),
    send: $("btnSend"),
    demo: $("btnDemo"),
    handoff: $("btnHandoff"),
    reset: $("btnReset"),
    trace: $("btnTrace"),
    traceClose: $("btnTraceClose"),
    tracePanel: $("tracePanel"),
    traceBody: $("traceBody"),
    traceFoot: $("traceFoot"),
    chatName: $("chatName"),
    chatMeta: $("chatMeta"),
    engineBadge: $("engineBadge"),
    statusLine: $("statusLine"),
    chips: $("chips"),
    search: $("search"),
    hintText: $("hintText"),
    skillModal: $("skillModal"),
    skillTitle: $("skillTitle"),
    skillName: $("skillName"),
    skillBody: $("skillBody"),
    skillClose: $("skillClose"),
  };

  const QUICK_CHIPS = [
    "我们的订单部署到哪一步了？",
    "系统多少钱一年？",
    "机器人答错价格了，怎么处理？",
    "我要投诉，转人工",
  ];

  const DEMO_SCRIPT = [
    { text: "你好，我们买的客服系统部署到哪一步了？", wait: 700 },
    { text: "那大概什么时候能上线？", wait: 700 },
    { text: "费用是多少？后面续费怎么算？", wait: 700 },
    { text: "还有个问题，昨天机器人把价格答错了，客户直接投诉了", wait: 900 },
  ];

  let appState = null;
  let currentId = "c1";
  let busy = false;
  let demoRunning = false;
  let activeTrace = null;
  const skillDetails = {};

  // ---------------- 网络 ----------------
  async function getState(customerId = currentId) {
    const res = await fetch(`/api/state?customer_id=${encodeURIComponent(customerId)}`);
    return res.json();
  }

  async function post(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data && data.error) throw new Error(data.error);
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadSkills() {
    try {
      const res = await fetch("/api/skills");
      const data = await res.json();
      (data.skills || []).forEach((skill) => {
        skillDetails[skill.name] = skill;
      });
    } catch (err) {
      console.warn("技能详情加载失败", err);
    }
  }

  // ---------------- 渲染 ----------------
  function escapeText(text) {
    return String(text ?? "");
  }

  function avatarEl(char, color, extraClass = "") {
    const el = document.createElement("div");
    el.className = `avatar ${extraClass}`.trim();
    el.textContent = char;
    if (!extraClass) el.style.background = color || "#8b8f96";
    return el;
  }

  function renderCustomers() {
    const keyword = els.search.value.trim();
    els.customerList.innerHTML = "";
    appState.customers
      .filter((c) => !keyword || (c.name + c.company).includes(keyword))
      .forEach((customer) => {
        const li = document.createElement("li");
        li.className = "customer" + (customer.id === currentId ? " active" : "");
        li.addEventListener("click", () => switchCustomer(customer.id));

        li.appendChild(avatarEl(customer.avatar, customer.color));

        const main = document.createElement("div");
        main.className = "customer-main";

        const top = document.createElement("div");
        top.className = "customer-top";
        const name = document.createElement("span");
        name.className = "customer-name";
        name.textContent = customer.name;
        const time = document.createElement("span");
        time.className = "customer-time";
        time.textContent = customer.last_time || "";
        top.append(name, time);

        const sub = document.createElement("div");
        sub.className = "customer-sub";
        sub.textContent = customer.last_message || customer.company;

        const tags = document.createElement("div");
        tags.style.marginTop = "4px";
        tags.style.display = "flex";
        tags.style.gap = "4px";

        const tag = document.createElement("span");
        tag.className = "mini-tag" + (customer.status === "human" ? " human" : "");
        tag.textContent = customer.status === "human" ? "人工接管" : customer.tag;
        tags.appendChild(tag);

        main.append(top, sub, tags);
        li.appendChild(main);
        els.customerList.appendChild(li);
      });
  }

  function renderHeader() {
    const customer = appState.customer;
    els.chatName.textContent = `${customer.name}${customer.company ? " · " + customer.company : ""}`;
    const runtime = appState.runtime;
    els.chatMeta.textContent = `${customer.title || ""} · ${runtime.company} ${runtime.agent_name}`;

    els.engineBadge.textContent = runtime.llm_enabled
      ? `LLM 引擎 · ${runtime.model}`
      : "离线规则引擎";
    els.engineBadge.classList.toggle("llm", runtime.llm_enabled);

    const isHuman = appState.session.status === "human";
    els.statusLine.textContent = isHuman ? "人工接管中（AI 已暂停自动回复）" : "AI 自动接待中";
    els.statusLine.classList.toggle("human", isHuman);
    els.handoff.textContent = isHuman ? "恢复 AI" : "转人工";
    els.input.placeholder = isHuman
      ? "以人工客服身份回复客户，Enter 发送"
      : "输入客户消息，Enter 发送，Shift + Enter 换行";
    els.hintText.textContent = isHuman
      ? "人工发言会以蓝色气泡记录，可随时点“恢复 AI”交还给智能体"
      : "提示：试试“我的订单部署到哪一步了”";
  }

  function messageEl(message) {
    const { role, text } = message;

    if (role === "system") {
      const wrap = document.createElement("div");
      wrap.className = "msg system";
      const pill = document.createElement("div");
      pill.className = "system-pill";
      pill.textContent = text;
      wrap.appendChild(pill);
      return wrap;
    }

    const isSelf = role !== "customer";
    const wrap = document.createElement("div");
    wrap.className = "msg" + (isSelf ? " self" : "") + (role === "human" ? " human" : "");

    if (role === "customer") {
      const customer = appState.customer;
      wrap.appendChild(avatarEl(customer.avatar || "客", customer.color));
    } else if (role === "human") {
      wrap.appendChild(avatarEl("人", null, "human-agent"));
    } else {
      wrap.appendChild(avatarEl("AI", null, "ai"));
    }

    const body = document.createElement("div");
    body.className = "msg-body";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = escapeText(text);
    body.appendChild(bubble);

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const time = document.createElement("span");
    time.textContent = message.time || "";
    meta.appendChild(time);

    if (role === "agent") {
      const label = document.createElement("span");
      label.textContent = `· ${message.engine || "AI"}${message.intent ? " · " + message.intent : ""}`;
      meta.appendChild(label);

      if (message.trace && message.trace.length) {
        const link = document.createElement("span");
        link.className = "trace-link";
        link.textContent = `查看轨迹(${message.trace.length})`;
        link.addEventListener("click", () => {
          showTrace(message);
          els.tracePanel.classList.add("open");
        });
        meta.appendChild(link);
      }
    } else if (role === "human") {
      const label = document.createElement("span");
      label.textContent = "· 人工客服";
      meta.appendChild(label);
    }

    body.appendChild(meta);
    wrap.appendChild(body);
    return wrap;
  }

  function renderMessages() {
    els.messages.innerHTML = "";
    const messages = appState.session.messages || [];
    messages.forEach((message) => els.messages.appendChild(messageEl(message)));
    scrollBottom();
  }

  function scrollBottom() {
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function typingEl() {
    const wrap = document.createElement("div");
    wrap.className = "msg self typing";
    wrap.id = "typingIndicator";
    wrap.appendChild(avatarEl("AI", null, "ai"));
    const body = document.createElement("div");
    body.className = "msg-body";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = '<span class="dot-anim"></span><span class="dot-anim"></span><span class="dot-anim"></span>';
    body.appendChild(bubble);
    wrap.appendChild(body);
    return wrap;
  }

  // ---------------- 轨迹面板 ----------------
  function stepClass(name) {
    if (name.includes("工具")) return "tool";
    if (name.includes("安全") || name.includes("兜底")) return "safety";
    if (name.includes("生成") || name.includes("大模型")) return "gen";
    return "";
  }

  function showTrace(message) {
    activeTrace = message;
    renderTrace();
  }

  function renderTrace() {
    els.traceBody.innerHTML = "";

    if (!activeTrace) {
      const empty = document.createElement("div");
      empty.className = "trace-empty";
      empty.textContent = "发送一条消息，这里会显示意图识别、Skill 路由、工具调用和生成过程。";
      els.traceBody.appendChild(empty);
      return;
    }

    const turn = document.createElement("div");
    turn.className = "trace-turn";

    const head = document.createElement("div");
    head.className = "trace-turn-head";
    const left = document.createElement("span");
    left.innerHTML = `<b>${activeTrace.intent || "会话"}</b> · ${activeTrace.time || ""}`;
    const right = document.createElement("span");
    right.textContent = activeTrace.engine || "";
    head.append(left, right);
    turn.appendChild(head);

    (activeTrace.trace || []).forEach((step) => {
      const item = document.createElement("div");
      item.className = "step " + stepClass(step.step);
      const name = document.createElement("span");
      name.className = "step-name";
      name.textContent = step.step;
      const detail = document.createElement("span");
      detail.className = "step-detail";
      detail.textContent = step.detail;
      item.append(name, detail);
      turn.appendChild(item);
    });

    if (activeTrace.text) {
      const reply = document.createElement("div");
      reply.className = "step gen";
      const name = document.createElement("span");
      name.className = "step-name";
      name.textContent = "最终回复";
      const detail = document.createElement("span");
      detail.className = "step-detail";
      detail.textContent = activeTrace.text;
      reply.append(name, detail);
      turn.appendChild(reply);
    }

    els.traceBody.appendChild(turn);

    const runtime = appState.runtime;
    els.traceFoot.innerHTML = "";
    const title = document.createElement("div");
    title.style.marginBottom = "4px";
    title.innerHTML = `<b>已加载 Skills（${runtime.skills.length}）</b><span class="skill-hint">点击查看 SKILL.md</span>`;
    els.traceFoot.appendChild(title);
    runtime.skills.forEach((skill) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "skill-row";
      row.title = `查看 ${skill.name} 的详细内容`;

      const code = document.createElement("code");
      code.textContent = skill.name;

      const right = document.createElement("span");
      right.style.display = "flex";
      right.style.alignItems = "center";
      right.style.gap = "6px";
      const desc = document.createElement("span");
      desc.textContent = skill.title;
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "›";
      right.append(desc, arrow);

      row.append(code, right);
      row.addEventListener("click", () => openSkill(skill.name));
      els.traceFoot.appendChild(row);
    });
  }

  // ---------------- Skill 详情弹层 ----------------
  function heading(text, level) {
    const h = document.createElement(level);
    h.textContent = text;
    return h;
  }

  function renderSkillDoc(text) {
    const frag = document.createDocumentFragment();
    let list = null;
    const flush = () => {
      if (list) {
        frag.appendChild(list);
        list = null;
      }
    };

    String(text || "").split("\n").forEach((raw) => {
      const line = raw.trim();
      if (!line) {
        flush();
        return;
      }
      if (line.startsWith("## ")) {
        flush();
        frag.appendChild(heading(line.slice(3), "h4"));
        return;
      }
      if (line.startsWith("# ")) {
        flush();
        frag.appendChild(heading(line.slice(2), "h4"));
        return;
      }
      const ordered = /^\d+[.、)]\s+/.test(line);
      const unordered = /^[-*]\s+/.test(line);
      if (ordered || unordered) {
        const tag = ordered ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== tag) {
          flush();
          list = document.createElement(tag);
        }
        const li = document.createElement("li");
        li.textContent = line.replace(/^\d+[.、)]\s+/, "").replace(/^[-*]\s+/, "");
        list.appendChild(li);
        return;
      }
      flush();
      const p = document.createElement("p");
      p.textContent = line;
      frag.appendChild(p);
    });
    flush();
    return frag;
  }

  function tagList(values, extraClass) {
    const box = document.createElement("div");
    box.className = "chip-list";
    (values || []).forEach((value) => {
      const tag = document.createElement("span");
      tag.className = "tag" + (extraClass ? " " + extraClass : "");
      tag.textContent = value;
      box.appendChild(tag);
    });
    return box;
  }

  function openSkill(name) {
    const skill = skillDetails[name];
    if (!skill) {
      console.warn("没有找到技能详情", name);
      return;
    }

    els.skillTitle.textContent = skill.title || skill.name;
    els.skillName.textContent = `skills/${skill.name}/SKILL.md`;
    els.skillBody.innerHTML = "";

    const desc = document.createElement("div");
    desc.className = "skill-desc";
    desc.textContent = skill.description || "（无描述）";
    els.skillBody.appendChild(desc);

    if (skill.triggers && skill.triggers.length) {
      const label = document.createElement("div");
      label.className = "skill-section-title";
      label.textContent = `触发词（${skill.triggers.length}）`;
      els.skillBody.append(label, tagList(skill.triggers));
    }

    if (skill.tools && skill.tools.length) {
      const label = document.createElement("div");
      label.className = "skill-section-title";
      label.textContent = "调用工具";
      els.skillBody.append(label, tagList(skill.tools, "tool"));
    }

    const ruleLabel = document.createElement("div");
    ruleLabel.className = "skill-section-title";
    ruleLabel.textContent = "执行流程与约束";
    els.skillBody.append(ruleLabel, renderSkillDoc(skill.body));

    els.skillModal.classList.add("open");
  }

  function closeSkill() {
    els.skillModal.classList.remove("open");
  }

  // ---------------- 交互 ----------------
  function applyState(state) {
    appState = state;
    renderCustomers();
    renderHeader();
    renderMessages();
  }

  async function switchCustomer(id) {
    if (busy) return;
    currentId = id;
    const state = await getState(id);
    const lastAgent = [...(state.session.messages || [])].reverse().find((m) => m.role === "agent");
    activeTrace = lastAgent || null;
    applyState(state);
    renderTrace();
  }

  async function sendMessage() {
    const text = els.input.value.trim();
    if (!text || busy) return;
    els.input.value = "";
    await dispatch(text);
  }

  function removeTyping() {
    const typing = $("typingIndicator");
    if (typing) typing.remove();
  }

  async function dispatch(text, options = {}) {
    const content = (text || "").trim();
    if (!content || busy) return;
    if (demoRunning && !options.fromDemo) return;

    busy = true;
    setButtonsDisabled(true);

    const isHuman = appState.session.status === "human";

    // 先本地回显：AI 接待时是客户发言，人工接管时是客服发言
    els.messages.appendChild(
      messageEl({ role: isHuman ? "human" : "customer", text: content, time: new Date().toTimeString().slice(0, 5) })
    );
    scrollBottom();

    if (!isHuman) {
      els.messages.appendChild(typingEl());
      scrollBottom();
    }

    try {
      const state = isHuman
        ? await post("/api/human-say", { customer_id: currentId, text: content })
        : await post("/api/chat", { customer_id: currentId, text: content });

      removeTyping();
      if (!isHuman) {
        const lastAgent = [...(state.session.messages || [])].reverse().find((m) => m.role === "agent");
        if (lastAgent) {
          activeTrace = lastAgent;
          renderTrace();
        }
      }
      applyState(state);
    } catch (err) {
      removeTyping();
      const reason = err.name === "AbortError" ? "请求超时（30 秒）" : err.message || "未知错误";
      els.messages.appendChild(
        messageEl({ role: "system", text: `发送失败：${reason}。请确认后端服务仍在运行后重试。` })
      );
      scrollBottom();
    } finally {
      removeTyping();
      busy = false;
      setButtonsDisabled(false);
      els.input.focus();
    }
  }

  function setButtonsDisabled(disabled) {
    [els.send, els.demo, els.handoff, els.reset].forEach((btn) => (btn.disabled = disabled));
    els.input.disabled = disabled;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function runDemo() {
    if (busy || demoRunning) return;
    demoRunning = true;
    els.demo.disabled = true;

    try {
      if (currentId !== "c1") await switchCustomer("c1");

      // 如果当前是人工接管状态，先切回 AI，否则演示脚本会被当成人工客服发言
      if (appState.session.status === "human") {
        const state = await post("/api/human", { customer_id: currentId, on: false });
        applyState(state);
      }

      for (const item of DEMO_SCRIPT) {
        els.input.value = item.text;
        await sleep(item.wait);
        els.input.value = "";
        await dispatch(item.text, { fromDemo: true });
        await sleep(600);
      }

      // 演示人工接管：投诉消息会触发 transfer_human，这里再补一句人工回复
      await sleep(1200);
      if (appState.session.status === "human") {
        await dispatch("您好，我是人工客服李工，价格答错的问题我已经看到了，正在核对知识库，20 分钟内给您答复。",
          { fromDemo: true });
      }
    } finally {
      demoRunning = false;
      els.demo.disabled = false;
    }
  }

  async function toggleHandoff() {
    if (busy) return;
    busy = true;
    const on = appState.session.status !== "human";
    const state = await post("/api/human", { customer_id: currentId, on });
    applyState(state);
    busy = false;
  }

  async function resetSession() {
    if (busy) return;
    busy = true;
    const state = await post("/api/reset", { customer_id: currentId });
    activeTrace = null;
    applyState(state);
    renderTrace();
    busy = false;
  }

  function buildChips() {
    els.chips.innerHTML = "";
    QUICK_CHIPS.forEach((text) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = text;
      chip.addEventListener("click", () => dispatch(text));
      els.chips.appendChild(chip);
    });
  }

  function bindEvents() {
    els.send.addEventListener("click", sendMessage);
    els.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    els.demo.addEventListener("click", runDemo);
    els.handoff.addEventListener("click", toggleHandoff);
    els.reset.addEventListener("click", resetSession);
    els.trace.addEventListener("click", () => els.tracePanel.classList.toggle("open"));
    els.traceClose.addEventListener("click", () => els.tracePanel.classList.remove("open"));
    els.search.addEventListener("input", renderCustomers);
    els.skillClose.addEventListener("click", closeSkill);
    els.skillModal.addEventListener("click", (event) => {
      if (event.target === els.skillModal) closeSkill();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeSkill();
    });
  }

  async function init() {
    buildChips();
    bindEvents();
    await loadSkills();
    const state = await getState();
    const messages = state.session.messages || [];
    const lastAgent = [...messages].reverse().find((m) => m.role === "agent");
    activeTrace = lastAgent || null;
    applyState(state);
    renderTrace();
    els.input.focus();
  }

  init();
})();
