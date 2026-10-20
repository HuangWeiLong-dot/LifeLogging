"""发布层的中文 → 英文翻译。

只影响**发到站点的副本**：本机 `events.db`、vault、qa 文件里的原文不动。
用现有的 DeepSeek（`common.LLM`），带本地缓存（`state/translation_cache.json`），
同一段文字只翻一次。

翻译范围是整个 payload 里的所有字符串值（对象键不翻）：
随手记、问答、任务、事件 value_text/meta、时间块 title、日记/vault Markdown、
聚合页的 AI 项目名 / 城市区名。URL、包名、文件路径、邮箱、ID、坐标、时间戳
都通过占位符保护，模型不会改动它们。

失败语义由调用方决定：
  · 原始归档严格 —— 有任何一段没翻成，就不发布，避免用中文覆盖已上线的英文版；
  · 聚合页也严格 —— 保留上一版英文，下一晚重试。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from common import CostLedger, LLM, TruncatedOutput, new_run_id

CJK_RE = re.compile(r"[一-鿿]")

SYSTEM = """You are the translation layer of a personal life-logging archive.
Translate every string in the input JSON array into natural, concise English.

Rules:
- Preserve Markdown syntax exactly: headings, list markers, task checkboxes,
  bold/italic, links, tables, blockquotes, code fences, and line breaks.
- Preserve URLs, file paths, package names, IDs, timestamps, numbers, and
  placeholders like __K0__ exactly. Do not translate or alter them.
- If a string is already English, return it unchanged.
- Do not add explanations, notes, or surrounding quotation marks.
- Return ONLY a JSON array of strings, same length and order as the input.
"""

RETRY_SYSTEM = SYSTEM + """
IMPORTANT: The output must contain NO Chinese characters at all.
If a string is Chinese, translate it fully; never echo or paraphrase it in Chinese.
Keep placeholders like __K0__ exactly as they are.
"""

# 不交给模型改写的东西。顺序有意义：URL 先吃掉整段，避免里面的 com.xxx 再命中。
PROTECT_RE = re.compile(
    r"(https?://[^\s<>\"')\]]+"
    r"|(?<![\w.-])com(?:\.[a-z0-9_]+)+"
    r"|[A-Za-z]:\\[^\s<>\"']+"
    r"|(?<![\w.-])[\w.+-]+@[\w.-]+\.\w+)",
    re.I,
)
PLACEHOLDER_RE = re.compile(r"__K(\d+)__")

MAX_BATCH_CHARS = 3000
MAX_BATCH_ITEMS = 15
CACHE_VERSION = "v1"

# 模型有时会保留中文标点（最常见的是【】）。它们不是汉字，has_cjk 抓不到，
# 但页面上看起来仍然"不是英文"，所以统一换成 ASCII。
PUNCT_MAP = str.maketrans({
    "【": "[", "】": "]",
    "（": "(", "）": ")",
    "，": ", ", "。": ". ",
    "：": ": ", "；": "; ",
    "、": ", ",
    "！": "! ", "？": "? ",
    "“": '"', "”": '"',
    "‘": "'", "’": "'",
    "《": "<", "》": ">",
    "「": '"', "」": '"',
    "『": '"', "』": '"',
})


def normalize_punctuation(text: str) -> str:
    return text.translate(PUNCT_MAP)


def has_cjk(text: Any) -> bool:
    return isinstance(text, str) and bool(CJK_RE.search(text))


class Translator:
    def __init__(
        self,
        cfg: dict[str, Any],
        state_dir: str | Path,
        max_calls: int = 30,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.failed = 0
        self.state_dir = Path(state_dir)
        self.cache_path = self.state_dir / "translation_cache.json"
        self.cache: dict[str, str] = {}
        self.retry: set[str] = set()
        self._dirty = False
        self._load_cache()

        if not enabled:
            self.llm = None
            self.run_id = ""
            return

        # 翻译是独立的一批调用：给它自己的单次上限，避免一个长回填撞上
        # 流水线默认的 8 次限制。每日金额上限仍然由 budget.daily_cap 管。
        tcfg = copy.deepcopy(cfg)
        budget = tcfg.setdefault("budget", {})
        budget["max_llm_calls_per_run"] = max(1, int(max_calls))
        ledger_path = budget.get("ledger_path") or (self.state_dir / "cost_ledger.jsonl")
        self.llm = LLM(tcfg, CostLedger(ledger_path))
        self.run_id = new_run_id()

    # ---------- cache ----------

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            data = raw.get("map", raw)
            if isinstance(data, dict):
                # 旧版本可能把模型原样回的中文也缓存了 —— 直接丢掉，重新翻
                self.cache = {
                    str(k): normalize_punctuation(str(v))
                    for k, v in data.items()
                    if not has_cjk(v)
                }

    def _save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"v": 1, "map": self.cache}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.cache_path)
        self._dirty = False

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(f"{CACHE_VERSION}|{text}".encode("utf-8")).hexdigest()

    # ---------- token protection ----------

    @staticmethod
    def _protect(text: str) -> tuple[str, dict[str, str]]:
        tokens: dict[str, str] = {}

        def repl(match: re.Match) -> str:
            raw = match.group(0)
            for key, value in tokens.items():
                if value == raw:
                    return key
            key = f"__K{len(tokens)}__"
            tokens[key] = raw
            return key

        return PROTECT_RE.sub(repl, text), tokens

    @staticmethod
    def _restore(text: str, tokens: dict[str, str]) -> str:
        return PLACEHOLDER_RE.sub(lambda m: tokens.get(m.group(0), m.group(0)), text)

    # ---------- LLM ----------

    @staticmethod
    def _strip_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _call(self, batch: list[str], system: str = SYSTEM) -> list[str] | None:
        protected = []
        maps = []
        for source in batch:
            text, tokens = self._protect(source)
            protected.append(text)
            maps.append(tokens)

        user = json.dumps(protected, ensure_ascii=False)
        out = self.llm.complete(
            system, user, stage="translate", run_id=self.run_id
        )
        out = self._strip_fences(out)

        data = None
        try:
            data = json.loads(out)
        except ValueError:
            # 模型偶尔会多说一句；退一步抓第一个 JSON 数组
            match = re.search(r"\[[\s\S]*\]", out)
            if match:
                try:
                    data = json.loads(match.group(0))
                except ValueError:
                    data = None

        if not isinstance(data, list) or len(data) != len(batch):
            return None

        result = []
        for translated, tokens in zip(data, maps):
            if not isinstance(translated, str):
                return None
            restored = normalize_punctuation(self._restore(translated, tokens))
            # 占位符没被原样保留 → 这一批不可信，交给上层重试/回退
            if PLACEHOLDER_RE.search(restored):
                return None
            result.append(restored)
        return result

    def _translate_chunk(self, chunk: list[str]) -> None:
        if not chunk:
            return
        try:
            translated = self._call(chunk)
        except TruncatedOutput as exc:
            if len(chunk) > 1:
                mid = len(chunk) // 2
                self._translate_chunk(chunk[:mid])
                self._translate_chunk(chunk[mid:])
            else:
                print(f"  ⚠️  translate truncated: {exc}")
                self.failed += 1
            return
        except Exception as exc:  # noqa: BLE001 — 预算/网络/401
            print(f"  ⚠️  translate failed ({len(chunk)} strings): {exc}")
            self.failed += len(chunk)
            return

        if translated is None:
            if len(chunk) > 1:
                mid = len(chunk) // 2
                self._translate_chunk(chunk[:mid])
                self._translate_chunk(chunk[mid:])
            else:
                # 解析失败的单个串交给第二轮（更强的提示）
                self.retry.add(chunk[0])
            return

        for source, english in zip(chunk, translated):
            # 模型偶尔会把某一条原样回中文。先记下来，等这一份文档 batch
            # 走完再统一用更强的提示重试，避免每条都单独打一次 API。
            if not english.strip() or has_cjk(english):
                self.retry.add(source)
                continue
            self.cache[self._key(source)] = english
            self._dirty = True
        self._save()

    def _retry_chunk(self, chunk: list[str]) -> None:
        if not chunk:
            return
        try:
            translated = self._call(chunk, RETRY_SYSTEM)
        except TruncatedOutput:
            if len(chunk) > 1:
                mid = len(chunk) // 2
                self._retry_chunk(chunk[:mid])
                self._retry_chunk(chunk[mid:])
            else:
                self.failed += 1
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  translate retry failed ({len(chunk)} strings): {exc}")
            self.failed += len(chunk)
            return

        if translated is None:
            if len(chunk) > 1:
                mid = len(chunk) // 2
                self._retry_chunk(chunk[:mid])
                self._retry_chunk(chunk[mid:])
            else:
                self._retry_single(chunk[0])
            return

        for source, english in zip(chunk, translated):
            if not english.strip() or has_cjk(english):
                self._retry_single(source)
                continue
            self.cache[self._key(source)] = english
            self._dirty = True
        self._save()

    def _retry_single(self, source: str) -> None:
        try:
            translated = self._call([source], RETRY_SYSTEM)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  translate retry failed: {exc}")
            self.failed += 1
            return
        if not translated or not translated[0].strip() or has_cjk(translated[0]):
            print(f"  ⚠️  still not English: {source[:60]!r}")
            self.failed += 1
            return
        self.cache[self._key(source)] = normalize_punctuation(translated[0])
        self._dirty = True
        self._save()

    def _retry_sources(self) -> None:
        sources = [s for s in self.retry if self._key(s) not in self.cache]
        self.retry.clear()
        if not sources:
            return
        for chunk in self._chunks(sources, max_chars=2500, max_items=10):
            self._retry_chunk(chunk)
        self._save()

    @staticmethod
    def _chunks(
        texts: list[str],
        max_chars: int = MAX_BATCH_CHARS,
        max_items: int = MAX_BATCH_ITEMS,
    ):
        current: list[str] = []
        size = 0
        for text in texts:
            if current and (size + len(text) > max_chars or len(current) >= max_items):
                yield current
                current, size = [], 0
            current.append(text)
            size += len(text)
        if current:
            yield current

    # ---------- public ----------

    def translate_many(self, texts: list[str]) -> list[str]:
        """返回与输入等长的英文列表；非中文原样返回。"""
        if not texts:
            return []
        if not self.enabled:
            return list(texts)

        missing: list[str] = []
        seen: set[str] = set()
        for text in texts:
            if not has_cjk(text) or text in seen:
                continue
            seen.add(text)
            if self._key(text) not in self.cache:
                missing.append(text)

        self.retry.clear()
        for chunk in self._chunks(missing):
            self._translate_chunk(chunk)
        if self.retry:
            self._retry_sources()
        self._save()

        out = []
        for text in texts:
            if not has_cjk(text):
                out.append(text)
            else:
                out.append(self.cache.get(self._key(text), text))
        return out

    def translate_text(self, text: str) -> str:
        if not has_cjk(text):
            return text
        return self.translate_many([text])[0]

    def translate_object(self, obj: Any) -> Any:
        """递归翻译所有字符串**值**（键不动），一次批量调用。"""
        if not self.enabled:
            return obj

        items: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str):
                items.append(value)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)

        collect(obj)
        if not items:
            return obj

        translated = self.translate_many(items)
        index = 0

        def rebuild(value: Any) -> Any:
            nonlocal index
            if isinstance(value, str):
                out = translated[index]
                index += 1
                return out
            if isinstance(value, list):
                return [rebuild(item) for item in value]
            if isinstance(value, dict):
                return {key: rebuild(item) for key, item in value.items()}
            return value

        return rebuild(obj)

    def translate_markdown(self, md: str) -> str:
        """只翻 Markdown 里的自然语言段落；代码围栏原样保留。"""
        if not self.enabled or not has_cjk(md):
            return md

        lines = md.splitlines(keepends=True)
        segments: list[tuple[str, str]] = []
        buffer: list[str] = []
        in_code = False

        for line in lines:
            if line.lstrip().startswith("```"):
                if buffer:
                    segments.append(("text", "".join(buffer)))
                    buffer = []
                segments.append(("code", line))
                in_code = not in_code
            elif in_code:
                segments.append(("code", line))
            else:
                buffer.append(line)
        if buffer:
            segments.append(("text", "".join(buffer)))

        # 文本段按空行切成段落；只有含中文的段落才翻
        split_segments: list[tuple[str, Any]] = []
        paragraphs: list[str] = []
        for kind, content in segments:
            if kind == "code":
                split_segments.append((kind, content))
                continue
            parts = re.split(r"(\n[ \t]*\n)", content)
            for part in parts:
                if part and not re.fullmatch(r"\n[ \t]*\n", part) and has_cjk(part):
                    paragraphs.append(part)
            split_segments.append((kind, parts))

        if not paragraphs:
            return md

        translated = self.translate_many(paragraphs)
        mapping = {src: en for src, en in zip(paragraphs, translated)}

        out: list[str] = []
        for kind, content in split_segments:
            if kind == "code":
                out.append(content)
            else:
                for part in content:
                    out.append(mapping.get(part, part))
        return "".join(out)
