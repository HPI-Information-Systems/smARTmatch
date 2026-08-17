"""
LLM fallback for dating strings the regex filter couldn't parse: batches
records to Qwen, parses its JSON mention output, and resolves it to a
(start, end) year interval via resolver.resolve_dating.
"""

import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from matching_pipeline.shared.env import get_model_config
from matching_pipeline.shared.llm_runtime import create_vllm, is_debug_enabled
from matching_pipeline.metadata_normalization.dating_normalization.resolver import resolve_dating
from matching_pipeline.metadata_normalization.dating_normalization.schema import Mention
from matching_pipeline.metadata_normalization.dating_normalization.structured_prompt import (
    build_structured_system_msg,
)

logger = logging.getLogger(__name__)

ITEMS_PER_PROMPT = 5
SYSTEM_MSG = build_structured_system_msg()


def build_prompt(entries: list[dict]) -> str:
    items = []
    for e in entries:
        item: dict = {"dating": e["dating"]}
        if e.get("author"):
            item["artist"] = e["author"]
        if e.get("date_of_birth"):
            item["artist_birth_year"] = e["date_of_birth"]
        if e.get("date_of_death"):
            item["artist_death_year"] = e["date_of_death"]
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def _to_int(value) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


_STRAY_PAREN_RE = re.compile(r"\](\s*)\)+(\s*)(?=[,\]])")
_EXTRA_BRACE_RE = re.compile(r"\]\}\}(\s*)(?=[,\]])")
_STRAY_QUOTE_AFTER_NUMBER_RE = re.compile(r'(:\s*-?\d+)"(?=[,}])')
_ECHOED_ARTIST_YEAR_RE = re.compile(r'"artist_(?:birth|death)_year":\s*"?-?\d+"?,?\s*')
_ECHOED_ARTIST_NAME_RE = re.compile(r'"artist":\s*"[^"]*",?\s*')

# Fix recurring glitches in the model's raw JSON output before parsing.
def _repair_json_glitches(text: str) -> str:
    text = _STRAY_PAREN_RE.sub(r"]\1}\2", text)
    text = _ECHOED_ARTIST_YEAR_RE.sub("", text)
    text = _ECHOED_ARTIST_NAME_RE.sub("", text)
    text = _STRAY_QUOTE_AFTER_NUMBER_RE.sub(r"\1", text)
    text = _EXTRA_BRACE_RE.sub(r"]}\1", text)
    return text


def parse_json_array_from_text(raw: str) -> list:
    # Try a straight parse, then slice at the outer brackets, then fall
    # back to extracting individual {...} objects by brace-depth.
    cleaned = raw.strip()
    cleaned = (
        cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    )
    cleaned = _repair_json_glitches(cleaned)

    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end > start:
        try:
            result = json.loads(cleaned[start : end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    search_area = cleaned[start : end + 1] if start != -1 and end > start else cleaned
    objects = []
    depth = 0
    obj_start = None
    for i, ch in enumerate(search_area):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    objects.append(json.loads(search_area[obj_start : i + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None

    if objects:
        return objects

    raise ValueError(f"Could not extract JSON array. Raw text: {cleaned[:300]}")


def mentions_from_raw(raw_mentions) -> list[Mention]:
    mentions = []
    if not isinstance(raw_mentions, list):
        return mentions
    for m in raw_mentions:
        if not isinstance(m, dict) or "kind" not in m:
            continue
        try:
            mentions.append(Mention.from_dict(m))
        except (TypeError, ValueError):
            continue
    return mentions


def _run_vllm(
    groups: list,
    model_name: str,
    quantization: Optional[str],
    device: str,
    gpu_memory_utilization: float,
    max_num_seqs: int,
    max_new_tokens: int,
) -> list[str]:
    from transformers import AutoTokenizer
    from vllm import SamplingParams

    llm = create_vllm(
        model=model_name,
        quantization=quantization,
        device=device,
        gpu_memory_utilization=gpu_memory_utilization,
        max_num_seqs=max_num_seqs,
        max_model_len=8192,
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    sampling_params = SamplingParams(temperature=0.0, max_tokens=max_new_tokens)

    prompts = []
    for group in groups:
        messages = [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": build_prompt(group)},
        ]
        prompts.append(
            tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        )

    outputs = llm.generate(prompts, sampling_params, use_tqdm=is_debug_enabled())
    return [o.outputs[0].text.strip() for o in outputs]


def _run_transformers(
    groups: list,
    model_name: str,
    device: str,
    max_new_tokens: int,
) -> list[str]:
    from transformers import AutoTokenizer, pipeline

    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    pipe = pipeline(
        "text-generation",
        model=model_name,
        tokenizer=tokenizer,
        device=device,
    )

    results = []
    for i, group in enumerate(groups, 1):
        messages = [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": build_prompt(group)},
        ]
        out = pipe(messages, max_new_tokens=max_new_tokens, do_sample=False)
        results.append(out[0]["generated_text"][-1]["content"].strip())
        if i % 10 == 0:
            logger.info(f"{i}/{len(groups)} groups done...")
    return results


def normalize_with_qwen(
    descriptions_file: Path,
    unmatched_file: Path,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    quantization: Optional[str] = None,
    device: Optional[str] = None,
    max_new_tokens: int = 1536,
) -> None:
    to_process = []
    with open(unmatched_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                to_process.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    logger.info(f"Records to process with Qwen: {len(to_process)}")

    qwen_results: dict = {}

    if to_process:
        config = get_model_config()
        backend = config.backend if backend is None else backend
        if backend not in {"vllm", "transformers"}:
            raise ValueError(f"Unsupported metadata backend: {backend!r}")

        model_name = config.model if model is None else model
        quant = config.quantization if quantization is None else quantization
        if isinstance(quant, str):
            quant = quant.strip() or None
        device_name = (config.device if device is None else device).strip().lower()
        if backend != "vllm":
            quant = None
        logger.info(
            f"Backend: {backend}  Model: {model_name}  "
            f"Quantization: {quant or 'none'}  Device: {device_name}"
        )
        if backend == "vllm":
            logger.info(
                "vLLM GPU memory utilization: %.2f  Max sequences: %d",
                config.gpu_memory_utilization,
                config.max_num_seqs,
            )

        groups = [
            to_process[i : i + ITEMS_PER_PROMPT]
            for i in range(0, len(to_process), ITEMS_PER_PROMPT)
        ]

        t0 = time.perf_counter()
        raw_outputs = (
            _run_vllm(
                groups,
                model_name,
                quant,
                device_name,
                config.gpu_memory_utilization,
                config.max_num_seqs,
                max_new_tokens,
            )
            if backend == "vllm"
            else _run_transformers(
                groups,
                model_name,
                device_name,
                max_new_tokens,
            )
        )
        dt = time.perf_counter() - t0

        n_warn = 0
        for group, raw in zip(groups, raw_outputs):
            try:
                parsed = parse_json_array_from_text(raw)
                if len(parsed) != len(group):
                    logger.warning(f"{len(parsed)} results for {len(group)} inputs")
                    n_warn += 1
            except Exception as exc:
                logger.warning(f"JSON parse failed ({type(exc).__name__}): {raw[:200]}")
                parsed = []
                n_warn += 1

            # Match parsed results back to input records by "dating" text,
            # since the model's output order/count isn't guaranteed to align.
            remaining_by_dating = defaultdict(list)
            for rec in group:
                remaining_by_dating[(rec.get("dating") or "").strip()].append(rec)

            matched = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                queue = remaining_by_dating.get(str(item.get("dating", "")).strip())
                if queue:
                    matched[queue.pop(0)["id"]] = item

            for rec in group:
                item = matched.get(rec["id"], {})
                raw_mentions = item.get("mentions", [])
                mentions = mentions_from_raw(raw_mentions)
                birth_year = _to_int(rec.get("date_of_birth"))
                start, end = resolve_dating(mentions, birth_year=birth_year)
                qwen_results[rec["id"]] = (start, end)

        speed = len(to_process) / dt if dt > 0 else 0.0
        logger.info(f"Inference: {dt:.2f}s  ({speed:.2f} entries/s)  Warnings: {n_warn}")

    # Read all records from descriptions_file, update the processed ones, write back
    all_records = []
    with open(descriptions_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                all_records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    n_ok = 0
    with open(descriptions_file, "w", encoding="utf-8") as f_out:
        for rec in all_records:
            if rec["id"] in qwen_results:
                start, end = qwen_results[rec["id"]]
                out_rec = {**rec, "dating_start": start, "dating_end": end}
            else:
                out_rec = rec
            if (
                out_rec.get("dating_start") is not None
                or out_rec.get("dating_end") is not None
            ):
                n_ok += 1
            f_out.write(json.dumps(out_rec, ensure_ascii=False) + "\n")

    logger.info(f"Records with interval: {n_ok} / {len(all_records)}")
