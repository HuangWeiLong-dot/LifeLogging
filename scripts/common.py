"""LifeLogging 公共模块：配置、事件表客户端、成本台账、LLM 封装。

所有脚本都从这里导入，改配置只改 config/config.yaml。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

# ── Windows 控制台 UTF-8 ────────────────────────────────────────────
# Windows 控制台默认走 GBK(cp936)，脚本里的 ✅⚠️❌ 会让 print 直接抛
# UnicodeEncodeError。所有脚本都 import 这个模块，所以在这里统一修一次。
# errors="replace" 保证即使真的编不出来也只是显示成 ?，不会中断流水线。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

# ── 路径 ────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.yaml"
CONFIG_EXAMPLE = ROOT / "config" / "config.example.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    if not p.exists():
        print(
            f"FAIL config file not found {p}\n"
            f"   Run: cp {CONFIG_EXAMPLE} {CONFIG_PATH}\n"
            f"   then fill in your API key and paths.",
            file=sys.stderr,
        )
        sys.exit(1)
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── 时区与日期 ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Day:
    """一天的本地时间范围，转成 UTC 用于查询。"""

    d: date
    tz: ZoneInfo

    @classmethod
    def parse(cls, s: str, tz: ZoneInfo) -> "Day":
        if s == "today":
            d = datetime.now(tz).date()
        elif s == "yesterday":
            d = datetime.now(tz).date() - timedelta(days=1)
        else:
            d = date.fromisoformat(s)
        return cls(d, tz)

    @property
    def iso(self) -> str:
        return self.d.isoformat()

    @property
    def start_local(self) -> datetime:
        return datetime.combine(self.d, time.min, tzinfo=self.tz)

    @property
    def end_local(self) -> datetime:
        return datetime.combine(self.d, time.max, tzinfo=self.tz)

    @property
    def start_utc(self) -> datetime:
        return self.start_local.astimezone(timezone.utc)

    @property
    def end_utc(self) -> datetime:
        return self.end_local.astimezone(timezone.utc)

    def utc_range_iso(self) -> tuple[str, str]:
        return (
            self.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            self.end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 事件表客户端 ────────────────────────────────────────────────────

@dataclass
class Event:
    """统一事件。domain 是宽泛的一级域，metric 是具体指标。

    例：
      domain="screen"    metric="app_usage"   value_num=秒数  value_text="VSCode"
      domain="location"  metric="visit"       value_num=停留分钟 value_text="家"
      domain="health"    metric="sleep"       value_num=小时   value_text=""
      domain="manual"    metric="caffeine"    value_num=2      value_text="杯"
    """

    ts_utc: str
    domain: str
    metric: str
    value_num: float | None = None
    value_text: str | None = None
    source: str = ""
    meta: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "ts_utc": self.ts_utc,
            "domain": self.domain,
            "metric": self.metric,
            "value_num": self.value_num,
            "value_text": self.value_text,
            "source": self.source,
            "meta": self.meta or {},
        }


class EventsClient:
    """中间件的 HTTP 客户端。"""

    def __init__(self, cfg: dict[str, Any]):
        ec = cfg["sources"]["events_api"]
        self.base = ec["api_base"].rstrip("/")
        self.key = ec["api_key"]
        self.headers = {"X-API-Key": self.key, "Content-Type": "application/json"}

    def post_batch(self, events: list[Event]) -> int:
        if not events:
            return 0
        total = 0
        # 分批，避免单请求过大
        for i in range(0, len(events), 500):
            chunk = [e.to_payload() for e in events[i : i + 500]]
            r = httpx.post(
                f"{self.base}/events/batch",
                headers=self.headers,
                json={"events": chunk},
                timeout=60.0,
            )
            r.raise_for_status()
            total += r.json().get("inserted", 0)
        return total

    def query(
        self,
        domain: str | None = None,
        metric: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if domain:
            params["domain"] = domain
        if metric:
            params["metric"] = metric
        if start:
            params["from"] = utc_iso(start)
        if end:
            params["to"] = utc_iso(end)
        r = httpx.get(
            f"{self.base}/events", headers=self.headers, params=params, timeout=60.0
        )
        r.raise_for_status()
        return r.json()["events"]

    def ping(self) -> bool:
        try:
            r = httpx.get(f"{self.base}/health", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False


# ── 成本台账 ────────────────────────────────────────────────────────


GENESIS = "genesis"


class CostLedger:
    """追加式 JSONL 台账。每行一次 LLM 调用。

    字段：ts_utc / day / run_id / stage / provider / model / peak /
          cache_hit / cache_miss / output / reasoning / cost / currency

    注意是 `cost` 不是 `cost_usd` —— 账户可能以人民币结算
    （DeepSeek 就是），币种由 `currency` 字段显式标明。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_chain(self) -> str:
        """读最后一行有效记录的 chain。没有就从创世值起。"""
        if not self.path.exists():
            return GENESIS
        last = None
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if not last:
            return GENESIS
        try:
            return json.loads(last).get("chain") or GENESIS
        except json.JSONDecodeError:
            return GENESIS

    def append(self, entry: dict[str, Any]) -> None:
        """追加一条记录，并接上 hash 链。

        链的意义：台账是这套系统里唯一碰钱的数据。如果它被静默改过或丢过行，
        后面所有预算判断都会是错的，而且不会有任何提示。
        链式哈希让这种情况**一定能被发现**（reconcile_cost.py 校验）。
        """
        entry.setdefault("ts_utc", utc_iso())
        prev = self._last_chain()
        payload = json.dumps(
            {k: v for k, v in entry.items() if k != "chain"},
            ensure_ascii=False,
            sort_keys=True,
        )
        entry["chain"] = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 半截行（进程被杀）直接跳过
        return out

    def spend_on(self, d: date, tz: ZoneInfo) -> float:
        """某本地日期的累计花费（USD）。

        优先用条目自带的 `day` 字段（写入方已经按本地时区归好日），
        只有在缺这个字段时才从 ts_utc 反推。
        不用 ts_utc 反推作为主路径：一次跨零点运行的 ts_utc 和它该归属的
        「哪一天」可能是两个答案，而写入方的判断才是对的。
        """
        want = d.isoformat()
        total = 0.0
        for e in self.entries():
            day = e.get("day")
            if day is None:
                try:
                    ts = datetime.fromisoformat(e["ts_utc"].replace("Z", "+00:00"))
                    day = ts.astimezone(tz).date().isoformat()
                except (KeyError, ValueError):
                    continue
            if day == want:
                total += float(e.get("cost", 0.0))
        return total

    def currency(self) -> str:
        """台账里用的币种（取自最近一条记录）。预算和上限都以它为准。"""
        for e in reversed(self.entries()):
            if e.get("currency"):
                return str(e["currency"])
        return "USD"


# ── 定价与成本计算 ──────────────────────────────────────────────────


@dataclass
class Usage:
    """归一化的 token 用量。

    各家 API 的字段名不一样（Anthropic 说 cache_read_input_tokens，
    DeepSeek 说 prompt_cache_hit_tokens），在各自的 provider 方法里转成这个形状，
    计价逻辑就只需要认识一种。
    """

    cache_hit: int = 0    # 命中缓存的输入 token（便宜很多）
    cache_miss: int = 0   # 未命中的输入 token（含首次写入缓存的）
    output: int = 0       # 输出 token
    reasoning: int = 0    # 其中的推理 token（信息性，已包含在 output 里）

    @property
    def total_in(self) -> int:
        return self.cache_hit + self.cache_miss


def is_peak(at: datetime | None = None) -> bool:
    """DeepSeek 峰谷计费判定。

    高峰 = 北京时间周一至周五 09:00-12:00 与 14:00-18:00，
    其余（含周末）为空闲时段，价格是高峰的一半。

    ⚠️ 未扣除法定节假日 —— 那需要一份节假日表，且最多让估算偏高一点点，
       对「预算封顶」这个目的来说偏保守是安全的。
    """
    bj = (at or now_utc()).astimezone(ZoneInfo("Asia/Shanghai"))
    if bj.weekday() >= 5:
        return False
    t = bj.time()
    return (time(9, 0) <= t < time(12, 0)) or (time(14, 0) <= t < time(18, 0))


def compute_cost(
    usage: Usage,
    model: str,
    pricing: dict[str, Any],
    at: datetime | None = None,
) -> dict[str, Any]:
    """按 price table 算钱。

    价格表支持两种写法：
      定值：   cache_hit: 0.02
      峰谷：   cache_hit: {peak: 0.04, off_peak: 0.02}

    ⚠️ 这是**本地估算**，不是账单。真实账单以厂商为准，
       由 reconcile_cost.py 每日核对。价格会变，记得定期更新。
    """
    p = pricing.get(model)
    if p is None:
        # 未知模型：不猜价格，但必须让调用方知道 ——
        # 悄悄按 0 计费会让预算控制完全失效。
        return {
            "cache_hit": usage.cache_hit,
            "cache_miss": usage.cache_miss,
            "output": usage.output,
            "reasoning": usage.reasoning,
            "cost": 0.0,
            "currency": pricing.get("_currency", "USD"),
            "peak": False,
            "unknown_pricing": 1.0,
        }

    peak = is_peak(at)
    key = "peak" if peak else "off_peak"

    def rate(field: str) -> float:
        v = p.get(field)
        if v is None:
            return 0.0
        if isinstance(v, dict):
            return float(v.get(key, v.get("off_peak", 0.0)))
        return float(v)

    cost = (
        usage.cache_hit * rate("cache_hit")
        + usage.cache_miss * rate("cache_miss")
        + usage.output * rate("output")
    ) / 1_000_000.0

    return {
        "cache_hit": usage.cache_hit,
        "cache_miss": usage.cache_miss,
        "output": usage.output,
        "reasoning": usage.reasoning,
        "cost": round(cost, 6),
        "currency": p.get("currency", "USD"),
        "peak": peak,
    }


# ── LLM 封装 ────────────────────────────────────────────────────────


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class LLM:
    """统一的 LLM 入口，内置预算预检与记账。

    provider 由 config.ai.provider 决定：
      deepseek    —— DeepSeek API（OpenAI 兼容协议），默认
      anthropic   —— Anthropic API
      claude_code —— 走 `claude -p --output-format json` 子进程
      ollama      —— 本地，成本恒为 0
    """

    cfg: dict[str, Any]
    ledger: CostLedger
    _calls: int = 0

    @property
    def provider(self) -> str:
        return self.cfg["ai"]["provider"]

    def _guard(self, stage: str) -> None:
        b = self.cfg["budget"]
        if self._calls >= int(b.get("max_llm_calls_per_run", 8)):
            raise BudgetExceeded(
                f"Max LLM calls for this run reached "
                f"({b.get('max_llm_calls_per_run')}); aborting to prevent a runaway loop."
            )
        tz = ZoneInfo(self.cfg["timezone"])
        spent = self.ledger.spend_on(datetime.now(tz).date(), tz)
        cap = float(b["daily_cap"])
        cur = b.get("currency", "CNY")
        if spent >= cap:
            msg = f"spent today {spent:.4f}, cap {cap:.2f} {cur}。"
            if b.get("fail_closed", True):
                raise BudgetExceeded(msg + " fail_closed=true, aborting.")
            print(f"⚠️  {msg} fail_closed=false, continuing.", file=sys.stderr)

    def complete(
        self,
        system: str,
        user: str,
        stage: str,
        run_id: str,
        cache_system: bool = True,
    ) -> str:
        self._guard(stage)
        self._calls += 1

        if self.provider == "deepseek":
            text, usage = self._deepseek(system, user)
        elif self.provider == "anthropic":
            text, usage = self._anthropic(system, user, cache_system)
        elif self.provider == "claude_code":
            text, usage = self._claude_code(system, user)
        elif self.provider == "ollama":
            text, usage = self._ollama(system, user)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

        model = self._model_name()
        stats = compute_cost(usage, model, self.cfg.get("pricing", {}))

        self.ledger.append(
            {
                "day": datetime.now(ZoneInfo(self.cfg["timezone"])).date().isoformat(),
                "run_id": run_id,
                "stage": stage,
                "provider": self.provider,
                "model": model,
                **stats,
            }
        )
        if stats.get("unknown_pricing"):
            print(
                f"WARN model {model} is not in config.pricing, so it is billed at 0. "
                f"Add it to the price table or budget control fails silently.",
                file=sys.stderr,
            )
        return text

    def _model_name(self) -> str:
        if self.provider == "ollama":
            return self.cfg["ai"]["ollama_model"]
        if self.provider == "deepseek":
            return self.cfg["ai"]["deepseek_model"]
        if self.provider == "claude_code":
            return f"claude-code:{self.cfg['ai'].get('model', 'default')}"
        return self.cfg["ai"]["model"]

    def _deepseek(self, system: str, user: str):
        """DeepSeek API —— OpenAI 兼容协议。

        和 Anthropic 的两个关键差别：
        1. **缓存是自动的**（磁盘前缀缓存），不需要也不支持 cache_control 标记。
           前缀 ≥1024 token 且逐字节匹配就自动命中，命中价约为未命中的 1/50。
           所以系统提示词越长、越稳定，越省钱 —— 不要在里头拼日期。
        2. **峰谷计费**：北京时间工作日 9-12、14-18 是高峰，其余半价。
           每晚 23:00 跑流水线正好落在空闲时段，直接省一半。
        """
        key = os.environ.get(self.cfg["ai"]["deepseek_api_key_env"])
        if not key:
            raise RuntimeError(
                f"Env var {self.cfg['ai']['deepseek_api_key_env']} is not set.\n"
                f"PowerShell, this session:  $env:{self.cfg['ai']['deepseek_api_key_env']}=\"sk-...\"\n"
                f"Permanent (new shells): setx {self.cfg['ai']['deepseek_api_key_env']} \"sk-...\""
            )
        base = self.cfg["ai"]["deepseek_base"].rstrip("/")

        r = httpx.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.cfg["ai"]["deepseek_model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": int(self.cfg["ai"]["max_output_tokens"]),
                "temperature": float(self.cfg["ai"]["temperature"]),
                "stream": False,
            },
            timeout=900.0,
        )
        if r.status_code == 401:
            raise RuntimeError("DeepSeek 401: API key is invalid or was rotated.")
        if r.status_code == 402:
            raise RuntimeError("DeepSeek 402: insufficient balance. Top up at platform.deepseek.com.")
        r.raise_for_status()

        d = r.json()
        if "error" in d:
            raise RuntimeError(f"DeepSeek error: {d['error']}")

        u = d.get("usage") or {}
        reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        # content 可能为 None（模型只产出了推理、没产出正文）
        text = (d["choices"][0]["message"].get("content") or "")

        return text, Usage(
            cache_hit=int(u.get("prompt_cache_hit_tokens", 0) or 0),
            cache_miss=int(u.get("prompt_cache_miss_tokens", 0) or 0),
            output=int(u.get("completion_tokens", 0) or 0),
            reasoning=int(reasoning or 0),
        )

    def _anthropic(self, system: str, user: str, cache_system: bool):
        import anthropic

        key = os.environ.get(self.cfg["ai"]["api_key_env"])
        if not key:
            raise RuntimeError(
                f"Env var {self.cfg['ai']['api_key_env']} is not set."
                f"PowerShell: $env:{self.cfg['ai']['api_key_env']}=\"sk-ant-...\""
            )
        client = anthropic.Anthropic(api_key=key)

        sys_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            # 系统提示词每天一样且很长，开缓存能省一大截
            sys_blocks[0]["cache_control"] = {"type": "ephemeral"}

        resp = client.messages.create(
            model=self.cfg["ai"]["model"],
            max_tokens=int(self.cfg["ai"]["max_output_tokens"]),
            temperature=float(self.cfg["ai"]["temperature"]),
            system=sys_blocks,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        u = resp.usage
        return text, Usage(
            cache_hit=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            # 缓存**写入**按未命中价计费 —— 首次把前缀写进缓存是要付钱的
            cache_miss=int(getattr(u, "input_tokens", 0) or 0)
            + int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            output=int(getattr(u, "output_tokens", 0) or 0),
        )

    def _claude_code(self, system: str, user: str):
        """复用 Claude Code 订阅。用 --append-system-prompt 注入系统提示。

        ⚠️ 返回的 usage 字段名在不同版本间有过变化（snake vs camel），
           这里做了兼容读取。首次使用前请实测一次：
             claude -p "hi" --output-format json | jq .usage
        """
        import subprocess

        proc = subprocess.run(
            [
                "claude",
                "-p",
                user,
                "--append-system-prompt",
                system,
                "--output-format",
                "json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=900,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p failed: {proc.stderr[:800]}")
        data = json.loads(proc.stdout)

        raw = data.get("usage", {}) or {}
        mixed = data.get("modelUsage", {}) or {}
        # modelUsage 里的键是模型名，取第一个
        agg = next(iter(mixed.values()), {}) if mixed else {}

        def pick(*names, default=0):
            for src in (raw, agg):
                for n in names:
                    if n in src and src[n] is not None:
                        return src[n]
            return default

        return data.get("result", ""), Usage(
            cache_hit=int(pick("cache_read_input_tokens", "cacheReadInputTokens")),
            cache_miss=int(pick("input_tokens", "inputTokens"))
            + int(pick("cache_creation_input_tokens", "cacheCreationInputTokens")),
            output=int(pick("output_tokens", "outputTokens")),
        )

    def _ollama(self, system: str, user: str):
        base = self.cfg["ai"]["ollama_base"].rstrip("/")
        r = httpx.post(
            f"{base}/api/chat",
            json={
                "model": self.cfg["ai"]["ollama_model"],
                "stream": False,
                "options": {"temperature": float(self.cfg["ai"]["temperature"])},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=900.0,
        )
        r.raise_for_status()
        data = r.json()

        # 本地推理零成本，仍然记 token 数，方便对比不同 provider 的上下文大小
        return data.get("message", {}).get("content", ""), Usage(
            cache_hit=0,
            cache_miss=int(data.get("prompt_eval_count", 0)),
            output=int(data.get("eval_count", 0)),
        )


# ── 小工具 ──────────────────────────────────────────────────────────


def cli_day(default: str = "today") -> str:
    """从 argv 里读 --date，缺省用 default。"""
    argv = sys.argv[1:]
    if "--date" in argv:
        i = argv.index("--date")
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_dirs(cfg: dict[str, Any]) -> None:
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)


def header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
