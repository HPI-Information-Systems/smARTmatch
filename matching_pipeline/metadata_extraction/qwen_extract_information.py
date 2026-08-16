#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM-based entity extraction: prompts Qwen with a fixed schema + few-shot
examples to pull title/author/dating/dimensions/etc. out of a raw auction
description, then parses and normalizes the model's JSON reply.
"""

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from matching_pipeline.shared.env import get_model_config
from matching_pipeline.shared.llm_runtime import create_vllm, is_debug_enabled

logger = logging.getLogger(__name__)
_PROGRESS_LOG_EVERY = 10

# ==== Entity definitions ====

SMARTMATCH_ENTITY_DESCRIPTIONS: Dict[str, str] = {
    "title": "Artwork title only – never the artist's birthplace or year.",
    "author": (
        "Artist name or attribution as stated, including any school/period/circle "
        "attribution (e.g. 'Französische Schule', 'Deutsche Schule') and qualifiers "
        "such as 'zugeschrieben', 'Umfeld von', 'Nachfolger von', 'Kreis von', "
        "'Schule von', 'Attributed to', 'Circle of', 'Follower of', 'Manner of', "
        "'Suiveur de', 'Surroundings of'. Never includes the artist's birthplace or "
        "birth/death year."
    ),
    "date_of_birth": "Artist year of birth.",
    "place_of_birth": "Artist place of birth.",
    "date_of_death": "Artist year of death.",
    "place_of_death": "Artist place of death.",
    "dimensions": "Dimensions string as written, e.g. '23,5 x 31 cm'. Also include dimensions of frame if stated.",
    "dating": (
        "Year or period the ARTWORK was created – never the artist's birth/death "
        "year. If no explicit artwork dating is stated but a period/century is "
        "given as part of an anonymous or school attribution (e.g. '1. Hälfte "
        "18. Jh.', 'um 1750', '19. Jahrhundert', '19th century'), use that period."
    ),
    "material": "Support/material, e.g. 'Holz', 'Papier', 'Lwd'.",
    "technique": "Technique/Medium, e.g. 'Öl', 'Mischtechnik', 'Aquarell'.",
    "provenance": "Ownership history as stated.",
    "signature": "Signature notation and placement as stated.",
    "condition": "Condition/restoration notes, or empty string. Also includes information about frame (except frame dimensions) if stated.",
    "literature": "Bibliography/reference sources after 'Lit.:', or empty string.",
}

LABELS: List[str] = list(SMARTMATCH_ENTITY_DESCRIPTIONS.keys())

OUTPUT_FIELDS: List[str] = [
    "title",
    "author",
    "date_of_birth",
    "place_of_birth",
    "date_of_death",
    "place_of_death",
    "dimensions",
    "dating",
    "material",
    "technique",
    "provenance",
    "signature",
    "condition",
    "literature",
]

# ==== Prompting / parsing helpers ====

FEW_SHOT_EXAMPLES = [
    {
        "input": 'Arnulf Rainer Baden 1929 Erotische Darstellung von Peter Fendi, aus der Pseudologica Serie Mischtechnik auf Druck auf Papier, auf Papier kaschiert 24,5 x 16 cm um 1987 rechts unten signiert: A. Rainer Literatur / literature: vgl. "Arnulf Rainer fissa Peter Fendi - Pseudologica" Hrsg. Peter Gorsen, Mailand 1989 Provenienz / provenance: Privatsammlung Wien',
        "output": {
            "title": "Erotische Darstellung von Peter Fendi, aus der Pseudologica Serie",
            "author": "Arnulf Rainer",
            "date_of_birth": "1929",
            "place_of_birth": "Baden",
            "date_of_death": "",
            "place_of_death": "",
            "dimensions": "24,5 x 16 cm",
            "dating": "um 1987",
            "material": "Papier, auf Papier kaschiert",
            "technique": "Mischtechnik auf Druck",
            "provenance": "Privatsammlung Wien",
            "signature": "rechts unten signiert: A. Rainer",
            "condition": "",
            "literature": 'vgl. "Arnulf Rainer fissa Peter Fendi - Pseudologica" Hrsg. Peter Gorsen, Mailand 1989',
        },
    },
    # Shows: no named artist, only a school/period attribution -> author = school, dating = the stated period
    {
        "input": "FRANZÖSISCHE SCHULE, um 1750.\nPorträt einer jungen Frau aus gutem Hause, die ihren Hund streichelt.\nÖl auf ovaler Leinwand. \n\nH. 85 x B. 68 cm \n\nAuf neue Leinwand gespannt und restauriert.",
        "output": {
            "title": "Porträt einer jungen Frau aus gutem Hause, die ihren Hund streichelt.",
            "author": "FRANZÖSISCHE SCHULE",
            "date_of_birth": "",
            "place_of_birth": "",
            "date_of_death": "",
            "place_of_death": "",
            "dimensions": "H. 85 x B. 68 cm",
            "dating": "um 1750",
            "material": "Leinwand",
            "technique": "Öl",
            "provenance": "",
            "signature": "",
            "condition": "Auf neue Leinwand gespannt und restauriert.",
            "literature": "",
        },
    },
    # Shows: school + "Umfeld von" + named artist combined into author
    {
        "input": "Deutsche Schule, um 1780, aus dem Umfeld von Anton GRAFF\nPorträt einer Frau\nLeinwand\n69 x 56 cm",
        "output": {
            "title": "Porträt einer Frau",
            "author": "Deutsche Schule, aus dem Umfeld von Anton GRAFF",
            "date_of_birth": "",
            "place_of_birth": "",
            "date_of_death": "",
            "place_of_death": "",
            "dimensions": "69 x 56 cm",
            "dating": "um 1780",
            "material": "Leinwand",
            "technique": "",
            "provenance": "",
            "signature": "",
            "condition": "",
            "literature": "",
        },
    },
    # Shows: English text, attribution qualifier, life dates vs artwork dating
    {
        "input": "Surroundings of Niccolo CASSANA (1659-1713)\nBust portrait of a court lady in a dress richly embroidered with floral motifs in gold thread, adorned with a pearl brooch at the front of the bodice.\nOil on canvas.\n73 x 61 cm.\nRepainting, restorations.\nGilded wood and stucco frame.",
        "output": {
            "title": "Bust portrait of a court lady in a dress richly embroidered with floral motifs in gold thread, adorned with a pearl brooch at the front of the bodice.",
            "author": "Surroundings of Niccolo CASSANA",
            "date_of_birth": "1659",
            "place_of_birth": "",
            "date_of_death": "1713",
            "place_of_death": "",
            "dimensions": "73 x 61 cm",
            "dating": "",
            "material": "canvas",
            "technique": "Oil",
            "provenance": "",
            "signature": "",
            "condition": "Repainting, restorations. Gilded wood and stucco frame.",
            "literature": "",
        },
    },
]


def build_few_shot_block() -> str:
    lines = ["EXAMPLES (input → correct output):"]
    for i, ex in enumerate(FEW_SHOT_EXAMPLES, 1):
        lines.append(f"\nExample {i}:")
        lines.append(f"INPUT: {ex['input']}")
        lines.append(f"OUTPUT: {json.dumps(ex['output'], ensure_ascii=False)}")
    return "\n".join(lines)


SYSTEM_MSG = f"""You are a careful information extraction assistant. You must follow instructions exactly and output ONLY valid JSON.

Extract entities from the given catalog entry.

Use these entity descriptions:
{json.dumps({"entity_descriptions": SMARTMATCH_ENTITY_DESCRIPTIONS}, ensure_ascii=False)}

OUTPUT RULES (must follow):
- Output ONLY a single JSON object, no markdown, no commentary.
- JSON format must be exactly with these keys: {json.dumps({k: "" for k in OUTPUT_FIELDS}, ensure_ascii=False)}
- Every key value must be a STRING.
- Use exact substrings from the input text when possible (keep original wording).
- If something is missing, output an empty string "".
- Do not hallucinate.

{build_few_shot_block()}"""


def build_user_prompt(text: str) -> str:
    return f"Now extract from this entry:\nINPUT TEXT:\n{text}"


def extract_json_object_strings(s: str) -> List[str]:
    """Return all valid JSON object substrings embedded in model output."""
    decoder = json.JSONDecoder()
    objects: List[str] = []
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            value, end = decoder.raw_decode(s[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(s[i : i + end])
    return objects


def extract_first_json_object(s: str) -> Optional[str]:
    objects = extract_json_object_strings(s.strip())
    return objects[0] if objects else None


_ws_re = re.compile(r"\s+")


def _norm_str(x: Any) -> str:
    if x is None:
        return ""
    return _ws_re.sub(" ", str(x).strip())


def _list_to_string(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        parts = [_norm_str(v) for v in val]
        parts = [p for p in parts if p]
        return " | ".join(parts) if parts else ""
    return _norm_str(val)


def normalize_output(obj: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {k: "" for k in OUTPUT_FIELDS}

    flat_hits = sum(1 for k in OUTPUT_FIELDS if k in obj)
    if flat_hits:
        for k in OUTPUT_FIELDS:
            out[k] = _list_to_string(obj.get(k, ""))
        return out

    ent = obj.get("entities", {})
    if isinstance(ent, dict):
        nested = ent.get("entities", ent)
        if isinstance(nested, dict):
            for k in OUTPUT_FIELDS:
                out[k] = _list_to_string(nested.get(k, ""))

    return out


def parse_jsonl_record_with_fallback(line: str) -> Optional[Dict[str, Any]]:
    try:
        rec = json.loads(line)
        return rec if isinstance(rec, dict) else None
    except json.JSONDecodeError:
        repaired = line.replace('\\\\"', '\\"')
        try:
            rec = json.loads(repaired)
            return rec if isinstance(rec, dict) else None
        except json.JSONDecodeError:
            return None


def _empty_entities() -> Dict[str, str]:
    return {k: "" for k in OUTPUT_FIELDS}


def _output_field_hit_count(obj: Dict[str, Any]) -> int:
    flat_hits = sum(1 for key in OUTPUT_FIELDS if key in obj)
    ent = obj.get("entities", {})
    if not isinstance(ent, dict):
        return flat_hits
    nested = ent.get("entities", ent)
    if not isinstance(nested, dict):
        return flat_hits
    return max(flat_hits, sum(1 for key in OUTPUT_FIELDS if key in nested))


def _entities_from_raw_output(raw: str) -> Dict[str, str]:
    """Parse the best JSON entity object from a raw model response.

    Qwen3 may emit reasoning before the final answer. That reasoning can contain
    the empty schema from the prompt, so using the first JSON object can silently
    discard a valid final JSON answer. Prefer the valid object with the most
    populated extraction fields, using the later object as a tie-breaker.
    """
    best_score = (-1, -1, -1)
    best_entities: Optional[Dict[str, str]] = None

    for index, json_str in enumerate(extract_json_object_strings(raw)):
        try:
            obj = json.loads(json_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        entities = normalize_output(obj)
        populated = sum(1 for key in OUTPUT_FIELDS if entities.get(key))
        field_hits = _output_field_hit_count(obj)
        if populated == 0 and field_hits == 0:
            continue

        score = (populated, field_hits, index)
        if score >= best_score:
            best_score = score
            best_entities = entities

    return best_entities if best_entities is not None else _empty_entities()


def _has_extracted_entities(entities: Dict[str, str]) -> bool:
    return any(entities.get(k) for k in OUTPUT_FIELDS)


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds:.0f}s"

    minutes, remaining_seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{remaining_seconds:02d}s"
    return f"{minutes}m{remaining_seconds:02d}s"


def _print_extraction_progress(
    processed: int,
    total: int,
    successful: int,
    start_time: float,
    record_id: Any,
) -> None:
    if processed % _PROGRESS_LOG_EVERY != 0 and processed != total:
        return

    elapsed = max(time.time() - start_time, 0.0)
    success_throughput = successful / elapsed if elapsed > 0 else 0.0
    processed_throughput = processed / elapsed if elapsed > 0 else 0.0
    remaining = max(total - processed, 0)
    eta = remaining / processed_throughput if processed_throughput > 0 else None
    pct = (processed / total * 100.0) if total else 100.0
    record_part = f" id={record_id}" if record_id is not None else ""

    logger.info(
        f"extraction progress: {processed}/{total} ({pct:.1f}%)"
        f" successful={successful}"
        f" throughput={success_throughput:.2f} successful texts/s"
        f" elapsed={_format_duration(elapsed)}"
        f" eta={_format_duration(eta)}"
        f"{record_part}"
    )


def _chat_text_from_messages(tokenizer: Any, messages: List[Dict[str, str]]) -> str:
    """Apply a chat template while disabling Qwen3 reasoning when supported."""
    kwargs: Dict[str, Any] = {
        "tokenize": False,
        "add_generation_prompt": True,
        "enable_thinking": False,
    }
    try:
        return tokenizer.apply_chat_template(messages, **kwargs)
    except TypeError:
        kwargs.pop("enable_thinking", None)
        return tokenizer.apply_chat_template(messages, **kwargs)


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("content", "text", "reasoning_content"):
            text = value.get(key)
            if text:
                return _norm_str(text)
        return ""
    return _norm_str(value)


def _pipeline_output_to_text(output: Any) -> str:
    """Normalize common transformers text-generation output shapes to text."""
    item = output[0] if isinstance(output, list) and output else output
    generated = item.get("generated_text", item.get("text", "")) if isinstance(item, dict) else item

    if isinstance(generated, str):
        return generated.strip()
    if isinstance(generated, dict):
        return _message_text(generated)
    if isinstance(generated, list):
        for message in reversed(generated):
            if isinstance(message, dict) and message.get("role") == "assistant":
                return _message_text(message)
        for message in reversed(generated):
            text = _message_text(message)
            if text:
                return text
    return _norm_str(generated)


def _run_vllm(
    records: List[Dict],
    model_name: str,
    quantization: Optional[str],
    device: str,
    gpu_memory_utilization: float,
    max_num_seqs: int,
    max_new_tokens: int,
) -> List[str]:
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
    for rec in records:
        messages = [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": build_user_prompt(rec["text"])},
        ]
        prompts.append(_chat_text_from_messages(tokenizer, messages))

    t0 = time.time()
    outputs = llm.generate(prompts, sampling_params, use_tqdm=is_debug_enabled())

    results = []
    n_ok = 0
    for i, (rec, output) in enumerate(zip(records, outputs), 1):
        raw = output.outputs[0].text.strip()
        results.append(raw)
        if _has_extracted_entities(_entities_from_raw_output(raw)):
            n_ok += 1
            _print_extraction_progress(i, len(records), n_ok, t0, rec.get("id"))
    return results


def _run_transformers(
    records: List[Dict], model_name: str, max_new_tokens: int
) -> List[str]:
    from transformers import AutoTokenizer, pipeline

    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    pipe = pipeline(
        "text-generation",
        model=model_name,
        tokenizer=tokenizer,
        trust_remote_code=True,
    )
    results = []
    n_ok = 0
    t0 = time.time()
    for i, rec in enumerate(records, 1):
        messages = [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": build_user_prompt(rec["text"])},
        ]
        prompt = _chat_text_from_messages(tokenizer, messages)
        out = pipe(
            prompt,
            max_new_tokens=max_new_tokens,
            max_length=None,
            do_sample=False,
            return_full_text=False,
        )
        raw = _pipeline_output_to_text(out)
        results.append(raw)
        if _has_extracted_entities(_entities_from_raw_output(raw)):
            n_ok += 1
            _print_extraction_progress(i, len(records), n_ok, t0, rec.get("id"))
    return results


def extract_metadata(
    descriptions_file: Path,
    model: Optional[str] = None,
    quantization: Optional[str] = None,
    max_new_tokens: int = 512,
    max_chars: int = 2000,
    backend: Optional[str] = None,
    device: Optional[str] = None,
    output_file: Optional[Path] = None,
) -> None:
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

    original_records = []
    inference_records = []
    n_skipped = 0
    with open(descriptions_file, "r", encoding="utf-8") as f_in:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            rec = parse_jsonl_record_with_fallback(line)
            if rec is None:
                n_skipped += 1
                continue
            text = rec.get("description")
            if text is None:
                text = rec.get("text", "")
            if not isinstance(text, str):
                text = "" if text is None else str(text)
            if max_chars and len(text) > max_chars:
                text = text[:max_chars]
            original_records.append(rec)
            inference_records.append({"id": rec.get("id"), "text": text})

    logger.info(
        f"Read {len(inference_records)} records from {descriptions_file} ({n_skipped} skipped)."
    )
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

    t0 = time.time()
    if backend == "vllm":
        raw_outputs = _run_vllm(
            inference_records,
            model_name,
            quant,
            device_name,
            config.gpu_memory_utilization,
            config.max_num_seqs,
            max_new_tokens,
        )
    else:
        raw_outputs = _run_transformers(inference_records, model_name, max_new_tokens)
    dt = time.time() - t0

    if len(raw_outputs) != len(original_records):
        raise RuntimeError(
            f"Inference returned {len(raw_outputs)} outputs for "
            f"{len(original_records)} input records."
        )

    target_file = descriptions_file if output_file is None else output_file
    n_ok = 0
    with open(target_file, "w", encoding="utf-8") as f_out:
        for orig, rec, raw in zip(original_records, inference_records, raw_outputs):
            entities = _entities_from_raw_output(raw)

            out_rec = dict(orig)
            for key in OUTPUT_FIELDS:
                val = entities.get(key, "")
                out_rec[key] = val if isinstance(val, str) else ""
            f_out.write(json.dumps(out_rec, ensure_ascii=False) + "\n")
            if _has_extracted_entities(entities):
                n_ok += 1

    speed = len(inference_records) / dt if dt > 0 else 0.0
    logger.info("=== Extraction done ===")
    logger.info(f"Processed:           {len(inference_records)}")
    logger.info(f"Successful entities: {n_ok}")
    logger.info(f"Time:                {dt:.2f}s  ({speed:.2f} texts/s)")
    if target_file != descriptions_file:
        logger.info(f"Output saved to:     {target_file}")
    if inference_records and n_ok == 0:
        raise RuntimeError(
            "Extraction produced no populated entities; aborting before "
            "downstream normalization/DB writes. Check model output format "
            "and prompt/model compatibility."
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        default="testset.jsonl",
        help="Path to input JSONL (fields: id, text)",
    )
    ap.add_argument(
        "--output",
        default="testset_labeled_qwen25_14B.jsonl",
        help="Path to output JSONL",
    )
    ap.add_argument("--model", default=None, help="HF model name or local path")
    ap.add_argument(
        "--quantization", default=None, help="Quantization method (vllm only)"
    )
    ap.add_argument("--max_new_tokens", type=int, default=512)
    ap.add_argument(
        "--max_chars",
        type=int,
        default=2000,
        help="If >0, truncate input text to this many characters",
    )
    ap.add_argument(
        "--backend",
        default=None,
        choices=["vllm", "transformers"],
        help="Inference backend (defaults to METADATA_BACKEND)",
    )
    ap.add_argument("--device", default=None, help="Inference device")
    args = ap.parse_args()

    extract_metadata(
        descriptions_file=Path(args.input),
        output_file=Path(args.output),
        model=args.model,
        quantization=args.quantization,
        max_new_tokens=args.max_new_tokens,
        max_chars=args.max_chars,
        backend=args.backend,
        device=args.device,
    )


if __name__ == "__main__":
    main()
