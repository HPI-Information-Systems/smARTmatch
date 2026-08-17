import json
import logging
import time
from pathlib import Path
from typing import Any, Optional
import re

from matching_pipeline.shared.env import get_model_config
from matching_pipeline.shared.llm_runtime import create_vllm, is_debug_enabled

logger = logging.getLogger(__name__)

_UNIT_TO_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54, "ft": 30.48}

_VALUE_UNIT_RE = re.compile(
    r"^\s*(-?\d+(?:[.,]\d+)?)\s*"
    r"(mm|cm|ft\.?|feet|foot|'|in\.?|inch(?:es)?|\"|'')\s*$",
    re.IGNORECASE,
)

_FEET_INCHES_RE = re.compile(
    r"^\s*(-?\d+(?:[.,]\d+)?)\s*(?:ft\.?|feet|foot|')\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:in\.?|inch(?:es)?|\"|'')?\s*$",
    re.IGNORECASE,
)

FIELDS = ["width", "height", "width_frame", "height_frame"]

EXAMPLES = [
    (
        "66 x 50 cm",
        '{"width": 50.0, "height": 66.0, "width_frame": null, "height_frame": null}',
    ),
    (
        "23,5 x 31 cm",
        '{"width": 31.0, "height": 23.5, "width_frame": null, "height_frame": null}',
    ),
    (
        "34.0 by 28.3 cm",
        '{"width": 28.3, "height": 34.0, "width_frame": null, "height_frame": null}',
    ),
    (
        "Frame 20 x 24 in. Image 16 x 20 in.",
        '{"width": "16 in.", "height": "20 in.", "width_frame": "24 in.", "height_frame": "20 in."}',
    ),
    (
        "height: 36 ¼ in.; 92 cm",
        '{"width": null, "height": 92.0, "width_frame": null, "height_frame": null}',
    ),
    (
        "H. 72 cm",
        '{"width": null, "height": 72.0, "width_frame": null, "height_frame": null}',
    ),
    (
        "ca. 44,5 cm Durchmesser",
        '{"width": 44.5, "height": 44.5, "width_frame": null, "height_frame": null}',
    ),
    (
        "53,5 x 76 cm, o. R.",
        '{"width": 76.0, "height": 53.5, "width_frame": null, "height_frame": null}',
    ),
    (
        "75,5 x 60 cm (Rahmen: 95 x 79 cm)",
        '{"width": 60.0, "height": 75.5, "width_frame": 79.0, "height_frame": 95.0}',
    ),
]

def _normalize_unit(unit: Optional[str]) -> str:
    if not unit:
        return "cm"
    u = unit.strip().lower().rstrip(".")
    if u in ("in", "inch", "inches", '"', "''"):
        return "in"
    if u in ("ft", "feet", "foot", "'"):
        return "ft"
    return u


def _convert_to_cm(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()

        fi_match = _FEET_INCHES_RE.match(text)
        if fi_match:
            feet_str, inches_str = fi_match.groups()
            feet = float(feet_str.replace(",", "."))
            inches = float(inches_str.replace(",", "."))
            return round(feet * _UNIT_TO_CM["ft"] + inches * _UNIT_TO_CM["in"], 3)

        match = _VALUE_UNIT_RE.match(text)
        if not match:
            logger.warning(f"Konnte Dimensionswert nicht parsen: {value!r}")
            return None
        number_str, unit = match.groups()
        number = float(number_str.replace(",", "."))
        factor = _UNIT_TO_CM.get(_normalize_unit(unit))
        if factor is None:
            logger.warning(f"Unbekannte Einheit in Dimensionswert: {value!r}")
            return None
        return round(number * factor, 3)
    return None


def _extract_json_objects(s: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(s[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _field_hit_count(obj: dict[str, Any]) -> int:
    return sum(1 for field in FIELDS if field in obj)


def _populated_field_count(obj: dict[str, Any]) -> int:
    return sum(1 for field in FIELDS if obj.get(field) is not None)


def _parse_dimensions(raw: str) -> Optional[dict[str, Any]]:
    best_score = (-1, -1, -1)
    best: Optional[dict[str, Any]] = None

    for index, obj in enumerate(_extract_json_objects(raw)):
        field_hits = _field_hit_count(obj)
        if field_hits == 0:
            continue
        parsed = {field: obj.get(field) for field in FIELDS}
        score = (_populated_field_count(parsed), field_hits, index)
        if score >= best_score:
            best_score = score
            best = parsed

    if best is not None:
        best = {field: _convert_to_cm(value) for field, value in best.items()}

    return best


def _chat_text_from_messages(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    kwargs: dict[str, Any] = {
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
                return str(text).strip()
        return ""
    return str(value).strip()


def _pipeline_output_to_text(output: Any) -> str:
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
    return str(generated).strip()


SYSTEM_MSG = f"""You are a precise data extraction assistant specializing in artwork dimensions. You extract numeric measurements from dimension strings. You output ONLY valid JSON, nothing else.

Extract artwork dimensions and return ONLY a JSON object with exactly these keys: {json.dumps({f: None for f in FIELDS})}

Rules:
- All values as numbers (float or null) if given in centimeters, otherwise as strings with unit (e.g. "20 in." for 20 inches)
- Dimension order is HEIGHT x WIDTH (first number = height, second = width)
- If both framed and unframed dimensions are given, extract both: unframed → width/height, framed → width_frame/height_frame
- If only one set of dimensions is given without framed/unframed context, use width/height (not width_frame/height_frame)
- If only a diameter is given (circular artwork), write the diameter value into both width and height
- Ignore depth (3rd dimension)
- If a value is not present, use null

Examples:
{chr(10).join(f'Input: "{inp}"\nOutput: {out}' for inp, out in EXAMPLES)}"""


def _build_prompt(text: str) -> str:
    return f'Input: "{text}"\nOutput:'


def _run_vllm(
    records: list[dict],
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
        max_model_len=4096,
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    sampling_params = SamplingParams(temperature=0.0, max_tokens=max_new_tokens)

    prompts = []
    for rec in records:
        messages = [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": _build_prompt(rec["dimensions"])},
        ]
        prompts.append(_chat_text_from_messages(tokenizer, messages))

    outputs = llm.generate(prompts, sampling_params, use_tqdm=is_debug_enabled())
    return [o.outputs[0].text.strip() for o in outputs]


def _run_transformers(
    records: list[dict],
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
        trust_remote_code=True,
    )

    results = []
    for i, rec in enumerate(records, 1):
        messages = [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": _build_prompt(rec["dimensions"])},
        ]
        prompt = _chat_text_from_messages(tokenizer, messages)
        out = pipe(
            prompt,
            max_new_tokens=max_new_tokens,
            max_length=None,
            do_sample=False,
            return_full_text=False,
        )
        results.append(_pipeline_output_to_text(out))
        if i % 50 == 0:
            logger.info(f"{i}/{len(records)} records done...")
    return results


def normalize_with_qwen(
    descriptions_file: Path,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    quantization: Optional[str] = None,
    device: Optional[str] = None,
    max_new_tokens: int = 128,
) -> None:
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

    to_process = [
        rec
        for rec in all_records
        if rec.get("dimensions")
        and not any(rec.get(f"dim_{field}") for field in FIELDS)
    ]
    logger.info(f"Records to process with Qwen: {len(to_process)}")

    qwen_results: dict[str, dict] = {}

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

        t0 = time.perf_counter()
        raw_outputs = (
            _run_vllm(
                to_process,
                model_name,
                quant,
                device_name,
                config.gpu_memory_utilization,
                config.max_num_seqs,
                max_new_tokens,
            )
            if backend == "vllm"
            else _run_transformers(
                to_process,
                model_name,
                device_name,
                max_new_tokens,
            )
        )
        dt = time.perf_counter() - t0

        n_warn = 0
        if len(raw_outputs) != len(to_process):
            raise RuntimeError(
                f"Dimension inference returned {len(raw_outputs)} outputs for "
                f"{len(to_process)} input records."
            )

        for rec, raw in zip(to_process, raw_outputs):
            parsed = _parse_dimensions(raw)
            if parsed is None:
                logger.warning(f"No JSON for: {rec.get('dimensions')!r}")
                n_warn += 1
                continue
            qwen_results[rec["id"]] = parsed

        speed = len(to_process) / dt if dt > 0 else 0.0
        logger.info(f"Inference: {dt:.2f}s  ({speed:.2f} entries/s)  Warnings: {n_warn}")

    n_ok = 0
    with open(descriptions_file, "w", encoding="utf-8") as f_out:
        for rec in all_records:
            if rec["id"] in qwen_results:
                parsed = qwen_results[rec["id"]]
                for field in FIELDS:
                    rec[f"dim_{field}"] = parsed.get(field)
            if rec.get("dim_width") is not None or rec.get("dim_height") is not None:
                n_ok += 1
            f_out.write(json.dumps(rec, ensure_ascii=False) + "\n")

    logger.info(f"Records with dimensions: {n_ok} / {len(all_records)}")
