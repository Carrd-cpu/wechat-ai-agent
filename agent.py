"""AI 客服 Agent 内核（零第三方依赖）

一次完整的 Agent 执行链路：
    安全策略 -> 意图识别 -> Skill 路由 -> 工具调用 -> 生成回复 -> 兜底 / 转人工

两种生成引擎，自动切换：
    * LLM 引擎：配置了 API Key 时启用，走标准 OpenAI 兼容的 function calling；
    * 离线引擎：没有 Key 或调用失败时自动降级，仍会真实执行 Skill 与工具，保证现场演示可用。

设计原则：Agent 的事实来源只有「工具返回值」和「知识库命中」，不允许模型自由发挥。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SKILLS_DIR = BASE_DIR / "skills"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _load_dotenv() -> None:
    """把 .env 里的 KEY=VALUE 读进环境变量，方便现场直接粘 Key。"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 知识库：关键词加权 + 字符二元组重合度（无需向量库）
# --------------------------------------------------------------------------- #
class KnowledgeBase:
    def __init__(self, items: list[dict]):
        self.items = items

    @classmethod
    def load(cls, path: Path) -> "KnowledgeBase":
        return cls(load_json(path, []))

    @staticmethod
    def _grams(text: str) -> set[str]:
        text = re.sub(r"[\s，。！？、,.!?：:；;（）()\[\]\"'“”]", "", text)
        return {text[i:i + 2] for i in range(len(text) - 1)}

    def search(self, query: str, top_k: int = 2, threshold: float = 1.2) -> list[tuple[dict, float]]:
        query_grams = self._grams(query)
        scored: list[tuple[dict, float]] = []
        for item in self.items:
            score = 0.0
            for kw in item.get("keywords", []):
                if kw and kw in query:
                    score += 2.0 + 0.2 * len(kw)
            if query_grams:
                doc_grams = self._grams(item["title"] + item.get("content", ""))
                score += 2.0 * len(query_grams & doc_grams) / len(query_grams)
            if score >= threshold:
                scored.append((item, round(score, 2)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


# --------------------------------------------------------------------------- #
# Skill：从 skills/*/SKILL.md 加载，支持 frontmatter 元信息
# --------------------------------------------------------------------------- #
class Skill:
    def __init__(self, name: str, meta: dict, body: str):
        self.name = name
        self.title = meta.get("title", name)
        self.description = meta.get("description", "")
        self.triggers = [t.strip() for t in re.split(r"[,，]", meta.get("triggers", "")) if t.strip()]
        self.tools = [t.strip() for t in re.split(r"[,，]", meta.get("tools", "")) if t.strip()]
        self.body = body

    def to_dict(self, with_body: bool = False) -> dict:
        data = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "triggers": self.triggers,
            "tools": self.tools,
        }
        if with_body:
            data["body"] = self.body
        return data


def parse_skill_file(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            body = parts[2].strip()
    return Skill(path.parent.name, meta, body)


class SkillRegistry:
    def __init__(self, skills: list[Skill]):
        self.skills = skills
        self.by_name = {s.name: s for s in skills}

    @classmethod
    def load(cls, root: Path) -> "SkillRegistry":
        skills = []
        if root.exists():
            for skill_file in sorted(root.glob("*/SKILL.md")):
                skills.append(parse_skill_file(skill_file))
        return cls(skills)

    def match(self, text: str) -> tuple[Skill | None, float]:
        """按触发词打分，返回得分最高的 Skill。"""
        best, best_score = None, 0.0
        for skill in self.skills:
            score = 0.0
            for trigger in skill.triggers:
                if trigger and trigger in text:
                    score += 1.0 + 0.1 * len(trigger)
            if score > best_score:
                best, best_score = skill, score
        return best, best_score

    def get(self, name: str) -> Skill | None:
        return self.by_name.get(name)


# --------------------------------------------------------------------------- #
# 工具：Agent 唯一的事实来源
# --------------------------------------------------------------------------- #
class ToolBox:
    def __init__(self, agent: "CustomerServiceAgent"):
        self.agent = agent

    # --- 订单查询 -----------------------------------------------------------
    def query_order(self, order_no: str = "", customer_id: str = "") -> dict:
        orders = self.agent.orders
        order_no = (order_no or "").strip().upper()
        if order_no:
            for order in orders:
                if order["order_no"].upper() == order_no:
                    # 只能查自己名下的订单，避免把别的客户订单信息返回出去
                    if customer_id and order["customer_id"] != customer_id:
                        return {"found": False, "reason": "order_not_belong_to_customer", "order_no": order_no}
                    return {"found": True, "match_by": "order_no", "order": order}
            return {"found": False, "reason": "order_no_not_exist", "order_no": order_no}

        candidates = [o for o in orders if o["customer_id"] == customer_id]
        if not candidates:
            return {"found": False, "reason": "no_order_for_customer"}
        active = [o for o in candidates if o["progress"] < 100] or candidates
        # 取进度最高的一笔未完成订单，通常就是客户最关心的那笔
        active.sort(key=lambda o: o["progress"], reverse=True)
        return {"found": True, "match_by": "customer", "order": active[0],
                "other_orders": [o["order_no"] for o in candidates if o is not active[0]]}

    # --- 知识库检索 ---------------------------------------------------------
    def search_knowledge(self, query: str = "") -> dict:
        hits = self.agent.kb.search(query or "")
        if not hits:
            return {"found": False, "results": []}
        return {
            "found": True,
            "results": [
                {"title": item["title"], "content": item["content"], "score": score}
                for item, score in hits
            ],
        }

    # --- 创建售后工单 -------------------------------------------------------
    def create_ticket(self, customer_id: str, issue: str = "", order_no: str = "", priority: str = "normal") -> dict:
        agent = self.agent
        seq = len(agent.tickets) + 1
        ticket_no = f"T{time.strftime('%Y%m%d')}{seq:03d}"
        ticket = {
            "ticket_no": ticket_no,
            "customer_id": customer_id,
            "order_no": order_no,
            "issue": (issue or "客户未描述具体问题")[:80],
            "priority": priority if priority in ("low", "normal", "high") else "normal",
            "status": "待受理",
            "created": now_str(),
        }
        agent.tickets.append(ticket)
        save_json(DATA_DIR / "tickets.json", agent.tickets)
        return {
            "created": True,
            "ticket_no": ticket_no,
            "priority": ticket["priority"],
            "sla": {"high": "30 分钟", "normal": "2 小时", "low": "1 个工作日"}[ticket["priority"]],
        }

    # --- 转人工 -------------------------------------------------------------
    def transfer_human(self, customer_id: str, reason: str = "", push_system: bool = True) -> dict:
        agent = self.agent
        session = agent.session(customer_id)
        session["status"] = "human"
        session["handoff_reason"] = (reason or "客户要求人工")[:120]
        if push_system:
            agent.push_message(customer_id, "system",
                               f"已转人工 · 原因：{session['handoff_reason']}（当前会话已暂停 AI 自动回复）")
        return {"ok": True, "status": "human", "service_hours": "人工在线 9:00-21:00"}

    # --- 统一调度（供 LLM function calling 使用） ---------------------------
    def call(self, name: str, args: dict) -> dict:
        args = args or {}
        try:
            if name == "query_order":
                return self.query_order(args.get("order_no", ""), args.get("customer_id", ""))
            if name == "search_knowledge":
                return self.search_knowledge(args.get("query", ""))
            if name == "create_ticket":
                return self.create_ticket(args.get("customer_id", ""), args.get("issue", ""),
                                          args.get("order_no", ""), args.get("priority", "normal"))
            if name == "transfer_human":
                return self.transfer_human(args.get("customer_id", ""), args.get("reason", ""))
        except Exception as exc:  # 工具异常不能让整条链路崩掉
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"error": f"unknown tool: {name}"}


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "查询订单状态、部署进度、交付时间和负责人。优先用订单号；客户没给订单号时用 customer_id 查其最近一笔未完成订单。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "description": "订单号，形如 ZW20261115001"},
                    "customer_id": {"type": "string", "description": "客户 ID，例如 c1"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索企业知识库，回答产品功能、价格、接入渠道、部署周期、数据安全、发票、试用、售后政策等问题。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "检索关键词或客户原问题"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "创建售后工单，用于客户反馈的故障、答错、功能异常等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "order_no": {"type": "string"},
                    "issue": {"type": "string", "description": "问题摘要，用客户原话概括"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                },
                "required": ["customer_id", "issue"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_human",
            "description": "转接人工客服。客户明确要求人工、投诉、退款争议、法律问题或 AI 无法解决时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "reason": {"type": "string", "description": "转接原因，包含客户诉求和已确认信息"},
                },
                "required": ["customer_id", "reason"],
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# 意图识别（规则版，成本低、可解释，面试时可现场演示加规则）
# --------------------------------------------------------------------------- #
INTENT_RULES = [
    ("转人工", ["投诉", "人工", "真人", "举报", "律师", "起诉", "找领导"]),
    ("订单查询", ["订单", "单号", "发货", "物流", "进度", "交付", "上线", "开通", "排期", "哪一步", "到哪了", "到哪一步"]),
    ("售后问题", ["报错", "故障", "用不了", "打不开", "答错", "不准", "异常", "崩溃", "售后", "工单", "坏了", "退款"]),
    ("价格咨询", ["多少钱", "价格", "收费", "费用", "报价", "预算", "贵", "便宜", "优惠", "年费"]),
    ("产品咨询", ["功能", "能做什么", "接入", "能接", "支持", "渠道", "小程序", "公众号", "部署", "安全", "发票", "试用", "案例", "培训", "介绍", "私有化"]),
    ("打招呼", ["你好", "您好", "在吗", "在么", "hello", "hi"]),
]


def detect_intent(text: str) -> tuple[str, float]:
    for intent, words in INTENT_RULES:
        for word in words:
            if word in text.lower():
                return intent, 0.9 if len(word) >= 2 else 0.6
    return "一般咨询", 0.55


ORDER_NO_RE = re.compile(r"[A-Za-z]{2}\d{9,}")
FOLLOWUP_RE = re.compile(r"多久|什么时候|进度|到哪|上线|完成|还要几")


# --------------------------------------------------------------------------- #
# Agent 主体
# --------------------------------------------------------------------------- #
class CustomerServiceAgent:
    def __init__(self, config_path: Path | None = None):
        _load_dotenv()
        self.config = load_json(config_path or BASE_DIR / "config.json", {})
        self.kb = KnowledgeBase.load(DATA_DIR / "knowledge.json")
        self.customers = {c["id"]: c for c in load_json(DATA_DIR / "customers.json", [])}
        self.orders = load_json(DATA_DIR / "orders.json", [])
        self.tickets = load_json(DATA_DIR / "tickets.json", [])
        self.skills = SkillRegistry.load(SKILLS_DIR)
        self.tools = ToolBox(self)
        self.sessions = load_json(DATA_DIR / "sessions.json", {})
        self._rate: dict[str, deque] = {}
        self._seq = 0

    # ---------------- 配置 ---------------- #
    @property
    def llm_key(self) -> str:
        return os.environ.get(self.config["llm"]["api_key_env"], "").strip()

    def runtime(self) -> dict:
        mode = self.config.get("mode", "auto")
        use_llm = mode in ("auto", "llm") and bool(self.llm_key)
        return {
            "mode": "LLM" if use_llm else "离线规则",
            "llm_enabled": use_llm,
            "model": self.config["llm"]["model"] if use_llm else "rule-engine",
            "agent_name": self.config["agent"]["name"],
            "company": self.config["agent"]["company"],
            "skills": [s.to_dict() for s in self.skills.skills],
        }

    # ---------------- 会话 ---------------- #
    def session(self, customer_id: str) -> dict:
        if customer_id not in self.sessions:
            self.sessions[customer_id] = {
                "customer_id": customer_id,
                "status": "ai",
                "messages": [],
                "last_order_no": "",
                "handoff_reason": "",
                "created": now_str(),
            }
        return self.sessions[customer_id]

    def _next_id(self) -> str:
        self._seq += 1
        return f"m{int(time.time() * 1000)}{self._seq}"

    def push_message(self, customer_id: str, role: str, text: str, **extra) -> dict:
        session = self.session(customer_id)
        message = {
            "id": self._next_id(),
            "role": role,          # customer / agent / human / system
            "text": text,
            "time": time.strftime("%H:%M"),
            "ts": now_str(),
        }
        message.update(extra)
        session["messages"].append(message)
        save_json(DATA_DIR / "sessions.json", self.sessions)
        return message

    def reset(self, customer_id: str) -> dict:
        self.sessions.pop(customer_id, None)
        save_json(DATA_DIR / "sessions.json", self.sessions)
        return self.state(customer_id)

    def state(self, customer_id: str) -> dict:
        customer = self.customers.get(customer_id, {"id": customer_id, "name": "客户"})
        session = self.session(customer_id)
        summary = []
        for cid, s in self.sessions.items():
            info = self.customers.get(cid, {})
            last = s["messages"][-1] if s["messages"] else None
            summary.append({
                "id": cid,
                "name": info.get("name", cid),
                "company": info.get("company", ""),
                "title": info.get("title", ""),
                "avatar": info.get("avatar", "客"),
                "color": info.get("color", "#8b8f96"),
                "tag": info.get("tag", ""),
                "status": s["status"],
                "last_message": (last["text"] if last else "还没有对话"),
                "last_time": (last["time"] if last else ""),
                "unread": 0,
            })
        # 没有对话的客户也列出来，方便演示时切换
        for cid, info in self.customers.items():
            if cid not in self.sessions:
                summary.append({**info, "status": "ai", "last_message": "还没有对话", "last_time": "", "unread": 0})
        summary.sort(key=lambda x: list(self.customers).index(x["id"]) if x["id"] in self.customers else 99)
        return {
            "customer": customer,
            "session": session,
            "customers": summary,
            "orders": [o for o in self.orders if o["customer_id"] == customer_id],
            "runtime": self.runtime(),
        }

    # ---------------- 安全策略 ---------------- #
    def _rate_limited(self, customer_id: str) -> bool:
        limit = int(self.config["safety"]["rate_limit_per_minute"])
        now = time.time()
        queue = self._rate.setdefault(customer_id, deque())
        while queue and now - queue[0] > 60:
            queue.popleft()
        if len(queue) >= limit:
            return True
        queue.append(now)
        return False

    def _hit_handoff_keyword(self, text: str) -> str:
        for keyword in self.config["safety"]["handoff_keywords"]:
            if keyword in text:
                return keyword
        return ""

    # ---------------- 主入口 ---------------- #
    def handle(self, customer_id: str, text: str) -> dict:
        text = (text or "").strip()
        started = time.time()
        session = self.session(customer_id)
        trace: list[dict] = []

        if not text:
            return {"silent": True}

        # 无论谁接待，客户消息都要落库
        self.push_message(customer_id, "customer", text)

        # 人工接管期间，AI 不抢话
        if session["status"] == "human":
            return {"silent": True, "reason": "human_takeover"}

        handoff_keyword = self._hit_handoff_keyword(text)
        intent, confidence = detect_intent(text)
        trace.append({"step": "意图识别", "detail": f"{intent}（置信度 {confidence:.0%}）"})

        # 1) 明确要人工，直接转
        if handoff_keyword:
            trace.append({"step": "安全策略", "detail": f"命中高风险关键词「{handoff_keyword}」"})
            trace.append({"step": "工具调用", "detail": "transfer_human"})
            reason = f"客户提到「{handoff_keyword}」，诉求：{text[:40]}"
            self.tools.transfer_human(customer_id, reason, push_system=False)
            reply = ("好的，这次我直接帮您转人工同事，请稍等。人工在线时间是 9:00-21:00，"
                     "紧急故障可以拨打技术支持电话。")
            self._finish(customer_id, reply, trace, "规则", started, intent)
            self.push_message(customer_id, "system", f"已转人工 · 原因：{reason}（AI 已暂停自动回复）")
            return {"ok": True}

        # 2) 频率限制
        if self._rate_limited(customer_id):
            trace.append({"step": "安全策略", "detail": "触发频率限制"})
            self._finish(customer_id, "您消息发得有点快，我缓一下，马上逐条回复～", trace, "规则", started, intent)
            return {"ok": True}

        # 3) Skill 路由（含上下文追问补全）
        skill, score = self.skills.match(text)
        if skill is None and session.get("last_order_no") and FOLLOWUP_RE.search(text):
            skill, score = self.skills.get("order-query"), 1.0
            trace.append({"step": "Skill 路由", "detail": "无显式触发词，结合上文订单语义补全 -> order-query"})
        else:
            trace.append({
                "step": "Skill 路由",
                "detail": f"{skill.name}（{skill.title}）· 得分 {score:.1f}" if skill else "未命中，按通用咨询处理",
            })
        if skill and skill.name == "human-handoff":
            trace.append({"step": "工具调用", "detail": "transfer_human"})
            reason = f"客户要求人工：{text[:40]}"
            self.tools.transfer_human(customer_id, reason, push_system=False)
            self._finish(customer_id, "已经帮您转接人工客服，同事会带着我们刚才的聊天记录接手，不用重复描述。",
                         trace, "规则", started, intent)
            self.push_message(customer_id, "system", f"已转人工 · 原因：{reason}（AI 已暂停自动回复）")
            return {"ok": True}

        # 4) 生成回复：LLM 优先，失败降级到离线引擎
        reply, engine = self._llm_reply(customer_id, text, skill, trace)
        if reply is None:
            reply, engine = self._offline_reply(customer_id, text, skill, intent, trace)

        self._finish(customer_id, reply, trace, engine, started, intent)
        return {"ok": True}

    def _finish(self, customer_id: str, reply: str, trace: list[dict],
                engine: str, started: float, intent: str) -> None:
        latency = int((time.time() - started) * 1000)
        trace.append({"step": "生成回复", "detail": f"{engine} 引擎 · {len(reply)} 字 · {latency} ms"})
        self.push_message(customer_id, "agent", reply, trace=trace, engine=engine, intent=intent)
        session = self.session(customer_id)
        session["updated"] = now_str()
        save_json(DATA_DIR / "sessions.json", self.sessions)

    # ---------------- 离线引擎 ---------------- #
    def _offline_reply(self, customer_id: str, text: str, skill: Skill | None,
                       intent: str, trace: list[dict]) -> tuple[str, str]:
        if intent == "打招呼" and not skill:
            company = self.config["agent"]["company"]
            name = self.config["agent"]["name"]
            return (f"您好，我是{company}的 AI 助手{name}～ 您可以直接问我订单进度、产品价格、"
                    f"售后问题，也可以说“转人工”找同事。"), "规则"

        if skill is None:
            return self._general_reply(customer_id, text, trace), "规则"

        handler = getattr(self, f"_skill_{skill.name.replace('-', '_')}", None)
        if handler is None:
            return self._general_reply(customer_id, text, trace), "规则"
        return handler(customer_id, text, trace), "规则"

    def _skill_order_query(self, customer_id: str, text: str, trace: list[dict]) -> str:
        order_no = ""
        match = ORDER_NO_RE.search(text)
        if match:
            order_no = match.group(0).upper()
        trace.append({"step": "工具调用", "detail": f"query_order(order_no={order_no or '-'}, customer_id={customer_id})"})

        result = self.tools.query_order(order_no=order_no, customer_id=customer_id)
        if not result.get("found"):
            if result.get("reason") == "order_not_belong_to_customer":
                trace.append({"step": "权限校验", "detail": f"{order_no} 不属于当前客户，已拒绝返回订单详情"})
                return (f"这笔订单不在您当前的账号名下，我这边看不到详情。"
                        f"如果是用其他账号下单的，麻烦用下单账号来问，或者我帮您转人工核对。")
            if result.get("reason") == "order_no_not_exist":
                return (f"系统里没查到订单 {order_no}，您方便核对一下单号吗？"
                        f"如果是别的账号下的单，也可以告诉我公司名称，我帮您查。")
            return "我这边需要订单号才能查到具体进度，您方便发一下吗？（形如 ZW20261115001）"

        order = result["order"]
        session = self.session(customer_id)
        session["last_order_no"] = order["order_no"]
        trace.append({"step": "工具返回", "detail": f"{order['order_no']} · {order['status']} · {order['progress']}%"})

        # 上一轮已经报过同一笔订单，这轮做追问式短答，避免复读
        told_before = any(m["role"] == "agent" and order["order_no"] in m["text"]
                          for m in session["messages"][-4:])
        if told_before and order["progress"] < 100:
            trace.append({"step": "上下文", "detail": "识别为对上一轮订单的追问，切换为短答"})
            return f"{order['eta']}。目前进度 {order['progress']}%，{order['owner']}在跟进，有节点变化我第一时间同步您。"

        if order["progress"] >= 100:
            return (f"订单 {order['order_no']}（{order['product']}）已经交付完成。{order['eta']}。"
                    f"使用中如果遇到问题，随时跟我说，我帮您开工单。")

        reply = (f"您的订单 {order['order_no']}（{order['product']}）目前是「{order['status']}」，"
                 f"进度 {order['progress']}%。{order['eta']}，由{order['owner']}负责跟进。")
        others = result.get("other_orders") or []
        if others:
            reply += f"另外您名下还有一笔 {others[0]} 在处理中，需要一起看吗？"
        return reply

    def _skill_after_sales(self, customer_id: str, text: str, trace: list[dict]) -> str:
        urgency_words = ["全部", "用不了", "打不开", "崩溃", "无法", "瘫痪", "所有"]
        priority = "high" if any(w in text for w in urgency_words) else "normal"

        session = self.session(customer_id)
        order_no = session.get("last_order_no", "")
        match = ORDER_NO_RE.search(text)
        if match:
            order_no = match.group(0).upper()

        issue = re.sub(r"^(你好|您好|在吗)[，,。 ]*", "", text)[:40]
        trace.append({"step": "工具调用",
                      "detail": f"create_ticket(customer_id={customer_id}, priority={priority})"})
        ticket = self.tools.create_ticket(customer_id, issue=issue, order_no=order_no, priority=priority)
        trace.append({"step": "工具返回", "detail": f"工单 {ticket['ticket_no']} 创建成功，SLA {ticket['sla']}"})

        return (f"已经帮您登记工单 {ticket['ticket_no']}，问题记录为「{issue}」，"
                f"优先级{ '高' if priority == 'high' else '普通' }。技术同事会在 {ticket['sla']} 内联系您，"
                f"工单进度我这边也能随时帮您查。")

    def _skill_product_consult(self, customer_id: str, text: str, trace: list[dict]) -> str:
        trace.append({"step": "工具调用", "detail": f"search_knowledge(query={text[:20]})"})
        result = self.tools.search_knowledge(text)
        if not result["found"]:
            trace.append({"step": "兜底策略", "detail": "知识库未命中"})
            self.tools.transfer_human(customer_id, f"知识库未覆盖的问题：{text[:40]}")
            return "这个问题我手头资料里没有准确答案，为了避免说错，我已经转给方案同事了，他会尽快联系您。"

        top = result["results"][0]
        trace.append({"step": "知识库命中", "detail": f"{top['title']}（{top['score']}）"})
        reply = top["content"]
        if top["title"] == "价格与收费方式":
            reply += " 您这边大概多少咨询量？我帮您对一下更合适的套餐。"
        elif top["title"] == "试用与 POC":
            reply += " 需要我帮您约一个 POC 排期吗？"
        return reply

    def _skill_human_handoff(self, customer_id: str, text: str, trace: list[dict]) -> str:
        trace.append({"step": "工具调用", "detail": "transfer_human"})
        self.tools.transfer_human(customer_id, f"客户要求人工：{text[:40]}")
        return "好的，已经为您转接人工客服，同事会带着我们刚才的聊天记录接手，您不用重复描述。"

    def _general_reply(self, customer_id: str, text: str, trace: list[dict]) -> str:
        trace.append({"step": "工具调用", "detail": "search_knowledge（通用兜底）"})
        result = self.tools.search_knowledge(text)
        if result["found"]:
            top = result["results"][0]
            trace.append({"step": "知识库命中", "detail": f"{top['title']}（{top['score']}）"})
            return top["content"]

        trace.append({"step": "兜底策略", "detail": "无命中，转人工并保留上下文"})
        self.tools.transfer_human(customer_id, f"AI 无法回答的问题：{text[:40]}")
        return ("这个问题我暂时没有准确答案，已经帮您转给人工同事，"
                "他可以看到我们前面的聊天记录。人工在线时间 9:00-21:00。")

    # ---------------- LLM 引擎 ---------------- #
    def _system_prompt(self, customer_id: str, skill: Skill | None) -> str:
        customer = self.customers.get(customer_id, {})
        orders = [o for o in self.orders if o["customer_id"] == customer_id]
        order_lines = "\n".join(
            f"- {o['order_no']}｜{o['product']}｜{o['status']}｜进度 {o['progress']}%｜{o['eta']}｜{o['owner']}"
            for o in orders
        ) or "- 该客户名下暂无订单"

        skill_block = ""
        if skill:
            skill_block = f"\n【本轮命中的 Skill：{skill.name}（{skill.title}）】\n{skill.body}\n"
        else:
            skill_block = "\n【本轮未命中专用 Skill，按通用咨询处理，必要时调用 search_knowledge】\n"

        all_skills = "；".join(f"{s.name}={s.title}" for s in self.skills.skills)

        return f"""你是{self.config['agent']['company']}的 AI 客服助手{self.config['agent']['name']}，
服务对象是已成交或在洽谈中的企业客户。今天是 {time.strftime('%Y-%m-%d')}。

【当前客户】
{customer.get('name', '客户')}｜{customer.get('company', '')}｜{customer.get('title', '')}
名下订单：
{order_lines}

【可用 Skills 总表】
{all_skills}
{skill_block}
【工作规则】
1. 涉及订单状态、进度、工单号的事实，必须先调用工具，禁止凭记忆回答。
2. 回答口语化、3 句话以内、不要用 markdown 列表和标题。
3. 工具和知识库都没有的信息，直接说需要人工同事确认，不要编造。
4. 客户提到投诉、退款、法律、明确要求人工时，调用 transfer_human。
5. 一次只回答一个问题，结尾最多带一个推进性问题。"""

    def _llm_reply(self, customer_id: str, text: str, skill: Skill | None,
                   trace: list[dict]) -> tuple[str | None, str]:
        llm = self.config["llm"]
        api_key = self.llm_key
        if self.config.get("mode", "auto") not in ("auto", "llm") or not api_key:
            return None, "离线规则"

        session = self.session(customer_id)
        history = session["messages"][-int(self.config["agent"]["history_in_prompt"]):-1]
        messages: list[dict] = [{"role": "system", "content": self._system_prompt(customer_id, skill)}]
        for message in history:
            if message["role"] == "customer":
                messages.append({"role": "user", "content": message["text"]})
            elif message["role"] in ("agent", "human"):
                messages.append({"role": "assistant", "content": message["text"]})
        messages.append({"role": "user", "content": text})

        for _ in range(int(self.config["agent"]["max_tool_rounds"])):
            data = self._chat_completion(messages, api_key)
            if data is None:
                trace.append({"step": "降级", "detail": "大模型调用失败，切换离线引擎"})
                return None, "离线规则"
            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = (message.get("content") or "").strip()
                trace.append({"step": "大模型生成", "detail": f"{llm['model']} · {len(content)} 字"})
                return content or None, "LLM"

            messages.append(message)
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                args.setdefault("customer_id", customer_id)
                trace.append({"step": "工具调用", "detail": f"{name}({json.dumps(args, ensure_ascii=False)})"})
                result = self.tools.call(name, args)
                trace.append({"step": "工具返回", "detail": json.dumps(result, ensure_ascii=False)[:120]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

        return "这个问题我需要人工同事确认一下，已经帮您转过去了。", "LLM"

    def _chat_completion(self, messages: list[dict], api_key: str) -> dict | None:
        llm = self.config["llm"]
        payload = {
            "model": llm["model"],
            "temperature": llm["temperature"],
            "messages": messages,
            "tools": TOOL_SPECS,
            "tool_choice": "auto",
        }
        request = urllib.request.Request(
            llm["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=llm["timeout"]) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError, OSError):
            return None
        return data if data.get("choices") else None


if __name__ == "__main__":
    agent = CustomerServiceAgent()
    print("Skills:", [s.name for s in agent.skills.skills])
    print("Runtime:", agent.runtime())
