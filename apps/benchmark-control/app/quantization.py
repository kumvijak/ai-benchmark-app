"""Streaming RTN checkpoints for the pinned vLLM 0.31 CPU compressed-tensors loader.

INT8 is W8A8: per-channel weights and dynamic per-token activations. INT4 is
W4A16: symmetric groups of 128, offset-binary nibbles packed into int32.
Only decoder attention/MLP Linear weights are quantized. Embeddings, norms,
biases, lm_head and the complete vision tower retain their original bytes.
This is weight reconstruction, not calibration or an accuracy evaluation.
No model is instantiated and no floating full-model copy is made.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import tempfile

SUPPORTED_MODELS = {
    "Qwen/Qwen3-VL-8B-Instruct": "qwen3_vl",
    "Qwen/Qwen3-4B-Base": "qwen3",
    "mistralai/Mistral-7B-v0.1": "mistral",
    "google/gemma-7b": "gemma",
}
PROJECTIONS = {"self_attn." + x for x in ("q_proj", "k_proj", "v_proj", "o_proj")} | {
    "mlp." + x for x in ("gate_proj", "up_proj", "down_proj")
}
DECODER = re.compile(r"^(?:model\.)?(?:language_model\.(?:model\.)?)?layers\.(\d+)\.(.+)$")
DTYPES = {"BF16": 2, "F16": 2, "F32": 4, "F64": 8, "I64": 8,
          "I32": 4, "I16": 2, "I8": 1, "U8": 1, "BOOL": 1}
IGNORE_ALIASES = ["re:.*visual.*", "re:.*vision.*", "re:.*lm_head.*"]
RECIPE = {"version": 2, "algorithm": "symmetric-minmax-round-to-nearest-even",
          "scale_denominator": "(2**bits-1)/2", "int4_group_size": 128,
          "int4_packing": "compressed-tensors-offset-binary-lsb-first-int32",
          "excluded": ["embeddings", "norms", "biases", "lm_head", "vision"],
          "ignore_aliases": IGNORE_ALIASES,
          "vllm": "0.31.0", "compressed_tensors": "0.17.0"}
RECIPE_SHA256 = hashlib.sha256(json.dumps(RECIPE, sort_keys=True).encode()).hexdigest()


def _identity(model_ref: str, revision: str, mode: str) -> None:
    if model_ref not in SUPPORTED_MODELS or mode not in {"int8", "int4"}:
        raise ValueError("Only the four approved models and int8/int4 are supported")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("An immutable 40-character source revision is required")


def build_prepare_command(model_ref: str, revision: str, mode: str,
                          output_dir: str | None = None,
                          script_path: str = "/tmp/aig-benchmark-control/quantization.py") -> str:
    """The API image can import this helper without installing Torch or NumPy."""
    _identity(model_ref, revision, mode)
    parts = ["python", script_path, "--model", model_ref, "--revision", revision, "--mode", mode]
    if output_dir:
        parts += ["--output-dir", output_dir]
    return " ".join(shlex.quote(x) for x in parts)


def _header(path: Path) -> tuple[dict, int]:
    with path.open("rb") as f:
        prefix = f.read(8)
        if len(prefix) != 8:
            raise ValueError("Truncated safetensors header")
        size = struct.unpack("<Q", prefix)[0]
        if size > 64 * 1024 * 1024 or size > path.stat().st_size - 8:
            raise ValueError("Invalid safetensors header length")
        def reject_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate safetensors key")
                result[key] = value
            return result
        header = json.loads(f.read(size), object_pairs_hook=reject_duplicates)
    end = 0
    for name, d in sorted(((n, d) for n, d in header.items() if n != "__metadata__"),
                          key=lambda item: item[1]["data_offsets"][0]):
        shape = d["shape"]
        if d["dtype"] not in DTYPES or any(not isinstance(x, int) or x < 0 for x in shape):
            raise ValueError("Invalid tensor dtype or shape")
        count = 1
        for dim in shape:
            count *= dim
        a, b = d["data_offsets"]
        if a != end or b - a != count * DTYPES[d["dtype"]]:
            raise ValueError("Invalid/overlapping safetensors offsets")
        end = b
    if 8 + size + end != path.stat().st_size:
        raise ValueError("Truncated or trailing safetensors data")
    return header, 8 + size


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _bf16_encode(x):
    import numpy as np
    u = np.asarray(x, dtype="<f4").view("<u4")
    return ((u + 0x7FFF + ((u >> 16) & 1)) >> 16).astype("<u2")


def _float_array(data: bytes, dtype: str, shape):
    import numpy as np
    if dtype == "BF16":
        return (np.frombuffer(data, dtype="<u2").astype("<u4") << 16).view("<f4").reshape(shape)
    if dtype not in {"F16", "F32"}:
        raise ValueError("Target decoder weights must be BF16/F16/F32")
    return np.frombuffer(data, dtype="<f2" if dtype == "F16" else "<f4").reshape(shape).astype("<f4")


def quantize_rows(weight, bits: int):
    """Quantize one bounded row block; layout matches CT 0.17 compressors."""
    import numpy as np
    if bits not in {4, 8} or weight.ndim != 2 or not np.isfinite(weight).all():
        raise ValueError("Invalid/nonfinite quantization input")
    n, k = weight.shape
    if bits == 4 and (k % 128 or n % 32):
        raise ValueError("INT4 CPU weights require K divisible128 and N divisible32")
    view = weight if bits == 8 else weight.reshape(n, k // 128, 128)
    scales = np.maximum(np.max(np.abs(view), axis=-1, keepdims=True) / ((2**bits - 1) / 2),
                        np.finfo(np.float32).eps).astype("<f4")
    if bits == 4:
        # Quantize against the scales actually serialized/consumed by BF16 vLLM.
        scales = (_bf16_encode(scales).astype("<u4") << 16).view("<f4")
    q = np.clip(np.rint(view / scales), -(2**(bits-1)), 2**(bits-1)-1).astype("i1").reshape(n, k)
    if bits == 8:
        return q.tobytes(), scales.reshape(n, 1).tobytes()
    unsigned = (q.astype("<i4") + 8).astype("<u4").reshape(n, k // 8, 8)
    packed = np.zeros((n, k // 8), dtype="<u4")
    for i in range(8):
        packed |= unsigned[:, :, i] << (4 * i)
    return packed.tobytes(), _bf16_encode(scales.reshape(n, k // 128)).tobytes()


class _ShardWriter:
    def __init__(self, path: Path):
        self.path = path
        self.header = {"__metadata__": {"format": "pt"}}
        self.data = tempfile.TemporaryFile(dir=path.parent)

    def tensor(self, name, dtype, shape, chunks):
        start = self.data.tell()
        for block in chunks:
            self.data.write(block)
        end = self.data.tell()
        expected = DTYPES[dtype]
        for dim in shape:
            expected *= dim
        if end - start != expected or name in self.header:
            raise ValueError("Invalid serialized tensor size/name")
        self.header[name] = {"dtype": dtype, "shape": list(shape), "data_offsets": [start, end]}

    def finish(self):
        header = json.dumps(self.header, separators=(",", ":")).encode()
        header += b" " * ((-len(header)) % 8)
        self.data.seek(0)
        with self.path.open("wb") as f:
            f.write(struct.pack("<Q", len(header)))
            f.write(header)
            shutil.copyfileobj(self.data, f, 1024 * 1024)
        self.data.close()


def _read_chunks(file, start, length, chunk_bytes):
    file.seek(start)
    while length:
        x = file.read(min(length, chunk_bytes))
        if not x:
            raise ValueError("Source tensor truncated during conversion")
        length -= len(x)
        yield x


def _module_kind(name: str, shape=None) -> tuple[bool, tuple | None]:
    if not name.endswith(".weight"):
        return False, None
    module = name[:-7]
    match = DECODER.fullmatch(module)
    if match:
        if match[2] not in PROJECTIONS:
            if shape is not None and len(shape) != 2:
                return False, None  # Decoder norms are not Linear weights.
            raise ValueError("Unknown decoder matrix must not fall back to BF16: " + name)
        return True, (int(match[1]), match[2])
    return False, None


def _config(mode, ignored):
    weights = {"num_bits": 8 if mode == "int8" else 4, "type": "int",
               "symmetric": True, "dynamic": False,
               "strategy": "channel" if mode == "int8" else "group"}
    if mode == "int4":
        weights["group_size"] = 128
    scheme = {"targets": ["Linear"], "weights": weights,
              "input_activations": None, "output_activations": None}
    if mode == "int8":
        scheme["input_activations"] = {"num_bits": 8, "type": "int", "symmetric": True,
                                        "strategy": "token", "dynamic": True}
    return {"quant_method": "compressed-tensors", "format": "int-quantized" if mode == "int8" else "pack-quantized",
            "quantization_status": "compressed", "config_groups": {"group_0": scheme},
            "ignore": sorted(set(ignored) | {"lm_head"} | set(IGNORE_ALIASES)), "kv_cache_scheme": None}


def prepare_checkpoint(source_directory: str | Path, model_ref: str, revision: str,
                       mode: str, output_directory: str | Path,
                       chunk_bytes: int = 64 * 1024 * 1024) -> dict:
    """Convert a pinned snapshot with bounded row buffers and atomic publication."""
    _identity(model_ref, revision, mode)
    if chunk_bytes < 1024 * 1024 or chunk_bytes > 256 * 1024 * 1024:
        raise ValueError("Chunk budget must be between1MiB and256MiB")
    source = Path(source_directory).resolve()
    output = Path(output_directory).resolve()
    if source == output or source in output.parents:
        raise ValueError("Output must be separate from the source checkpoint")
    if output.exists():
        existing = validate_checkpoint(output)
        if (existing["model_ref"], existing["source_revision"], existing["mode"], existing["recipe_sha256"]) != (
                model_ref, revision, mode, RECIPE_SHA256):
            raise ValueError("Existing checkpoint identity mismatch")
        return existing
    config = json.loads((source / "config.json").read_text())
    if config.get("quantization_config") or config.get("model_type") != SUPPORTED_MODELS[model_ref]:
        raise ValueError("Source must be the expected unquantized architecture")
    depth = (config.get("text_config") or config)["num_hidden_layers"]
    index_path = source / "model.safetensors.index.json"
    index = json.loads(index_path.read_text())["weight_map"] if index_path.exists() else None
    files = sorted(set(index.values())) if index else ["model.safetensors"]
    for filename in files:
        if Path(filename).name != filename or not filename.endswith(".safetensors"):
            raise ValueError("Unsafe shard filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".preparing-", dir=output.parent))
    proof = {"model_ref": model_ref, "source_revision": revision, "mode": mode,
             "bits": 8 if mode == "int8" else 4, "format": "int-quantized" if mode == "int8" else "pack-quantized",
             "recipe_sha256": RECIPE_SHA256, "recipe": RECIPE, "chunk_bytes": chunk_bytes,
             "peak_float_block_bytes": 0, "quantized_tensors": [], "preserved_tensors": [], "source_shards": {}}
    weight_map = {}
    coverage = set()
    seen = set()
    ignored = []
    total_size = 0
    temporary_files = []
    try:
        for filename in files:
            path = source / filename
            header, offset = _header(path)
            proof["source_shards"][filename] = _sha(path)
            writer = _ShardWriter(staging / filename)
            temporary_files.append(writer.data)
            with path.open("rb") as f:
                for name, d in header.items():
                    if name == "__metadata__":
                        continue
                    if name in seen or (index is not None and index.get(name) != filename):
                        raise ValueError("Source index mismatch/duplicate tensor")
                    seen.add(name)
                    target, layer = _module_kind(name, d["shape"])
                    a, b = d["data_offsets"]
                    if target:
                        n, k = d["shape"]
                        if n % 32 or (mode == "int4" and k % 128):
                            raise ValueError("Unsupported CPU decoder linear dimensions")
                        # Budget conservatively bounds FP32, scale, integer packing temporaries.
                        rows = max(32, (chunk_bytes // (k * 32) // 32) * 32)
                        if rows * k * 32 > chunk_bytes:
                            raise ValueError("Chunk budget cannot fit a32-row linear block")
                        qdata = tempfile.TemporaryFile(dir=staging)
                        scales = tempfile.TemporaryFile(dir=staging)
                        temporary_files.extend([qdata, scales])
                        for first in range(0, n, rows):
                            count = min(rows, n-first)
                            f.seek(offset + a + first * k * DTYPES[d["dtype"]])
                            data = f.read(count * k * DTYPES[d["dtype"]])
                            block = _float_array(data, d["dtype"], (count, k))
                            proof["peak_float_block_bytes"] = max(proof["peak_float_block_bytes"], block.nbytes)
                            q, scale = quantize_rows(block, proof["bits"])
                            qdata.write(q); scales.write(scale)
                            del block, data, q, scale
                        module = name[:-7]
                        qname = name if mode == "int8" else module + ".weight_packed"
                        qsize = qdata.tell(); ssize = scales.tell()
                        writer.tensor(qname, "I8" if mode == "int8" else "I32", (n, k if mode == "int8" else k//8),
                                      _read_chunks(qdata, 0, qsize, chunk_bytes))
                        writer.tensor(module + ".weight_scale", "F32" if mode == "int8" else "BF16",
                                      (n, 1 if mode == "int8" else k//128), _read_chunks(scales, 0, ssize, chunk_bytes))
                        qdata.close(); scales.close()
                        if mode == "int4":
                            writer.tensor(module + ".weight_shape", "I64", (2,), [struct.pack("<qq", n, k)])
                        proof["quantized_tensors"].append({"source_name": name, "stored_name": qname,
                                                            "shape": [n,k], "stored_dtype": "I8" if mode == "int8" else "I32"})
                        coverage.add(layer)
                    else:
                        if name.endswith(".weight"):
                            ignored.append(name[:-7])
                        checksum = hashlib.sha256()
                        def preserved_chunks():
                            for block in _read_chunks(f, offset+a, b-a, chunk_bytes):
                                checksum.update(block)
                                yield block
                        writer.tensor(name, d["dtype"], d["shape"], preserved_chunks())
                        proof["preserved_tensors"].append({"name":name,"dtype":d["dtype"],
                                                            "shape":d["shape"],"sha256":checksum.hexdigest()})
            writer.finish()
            for name, desc in writer.header.items():
                if name != "__metadata__":
                    weight_map[name] = filename
                    total_size += desc["data_offsets"][1] - desc["data_offsets"][0]
        if index is not None and seen != set(index):
            raise ValueError("Source index contains missing tensors")
        if coverage != {(i,p) for i in range(depth) for p in PROJECTIONS}:
            raise ValueError("Incomplete decoder Linear coverage; no BF16 fallback permitted")
        # Copy tokenizer/config auxiliary files only, never source weights/bin/pickle.
        for path in source.iterdir():
            if path.is_file() and (path.suffix in {".json", ".txt", ".model", ".jinja"} or path.name == "merges.txt"):
                if path.name not in {"config.json", "model.safetensors.index.json"}:
                    shutil.copyfile(path, staging/path.name)
        config["quantization_config"] = _config(mode, ignored)
        (staging/"config.json").write_text(json.dumps(config, indent=2)+"\n")
        (staging/"model.safetensors.index.json").write_text(json.dumps({"metadata":{"total_size":total_size},"weight_map":weight_map},indent=2)+"\n")
        proof["file_sha256"] = {p.name:_sha(p) for p in staging.iterdir() if p.is_file()}
        (staging/"quantization-manifest.json").write_text(json.dumps(proof,indent=2)+"\n")
        result = validate_checkpoint(staging)
        os.rename(staging, output)
        result["checkpoint_path"] = str(output)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        for handle in temporary_files:
            handle.close()


def validate_checkpoint(path: str | Path) -> dict:
    """Fail closed on corrupted/floating targeted storage or missing scales."""
    root = Path(path).resolve()
    proof = json.loads((root/"quantization-manifest.json").read_text())
    _identity(proof["model_ref"], proof["source_revision"], proof["mode"])
    if (proof["recipe_sha256"] != RECIPE_SHA256 or not proof["quantized_tensors"]
            or proof["bits"] != (8 if proof["mode"] == "int8" else 4)):
        raise ValueError("Unrecognized/empty quantization recipe")
    for name, digest in proof["file_sha256"].items():
        if Path(name).name != name or _sha(root/name) != digest:
            raise ValueError("Checkpoint file checksum mismatch")
    model_config = json.loads((root/"config.json").read_text())
    if model_config["model_type"] != SUPPORTED_MODELS[proof["model_ref"]]:
        raise ValueError("Checkpoint architecture mismatch")
    config = model_config["quantization_config"]
    index = json.loads((root/"model.safetensors.index.json").read_text())["weight_map"]
    expected = _config(proof["mode"], config["ignore"])
    if config != expected or config["format"] != proof["format"]:
        raise ValueError("Quantization metadata mismatch")
    files = set(index.values())
    if any(Path(name).name != name for name in files):
        raise ValueError("Unsafe checkpoint index")
    if not files | {"config.json","model.safetensors.index.json"} <= set(proof["file_sha256"]):
        raise ValueError("Checkpoint checksum manifest incomplete")
    parsed = {name:_header(root/name) for name in files}
    headers = {name:item[0] for name,item in parsed.items()}
    if {n for h in headers.values() for n in h if n != "__metadata__"} != set(index):
        raise ValueError("Checkpoint index and serialized tensors differ")
    coverage = set()
    stored_names = set()
    for item in proof["quantized_tensors"]:
        name = item["stored_name"]; n,k = item["shape"]; module = item["source_name"][:-7]
        target, layer = _module_kind(item["source_name"], [n,k])
        if not target or name in stored_names or n % 32 or (proof["mode"] == "int4" and k % 128):
            raise ValueError("Invalid/duplicate targeted matrix")
        stored_names.add(name); coverage.add(layer)
        if name != (item["source_name"] if proof["mode"] == "int8" else module+".weight_packed"):
            raise ValueError("Quantized tensor naming mismatch")
        header = headers[index[name]]; dtype = "I8" if proof["mode"] == "int8" else "I32"
        if header[name]["dtype"] != dtype or header[name]["shape"] != [n,k if dtype=="I8" else k//8]:
            raise ValueError("Targeted weight is not genuine integer storage")
        scale = headers[index[module+".weight_scale"]][module+".weight_scale"]
        if scale["dtype"] != ("F32" if dtype=="I8" else "BF16") or scale["shape"] != [n,1 if dtype=="I8" else k//128]:
            raise ValueError("Missing/invalid quantization scales")
        if dtype=="I32" and item["source_name"] in index:
            raise ValueError("Floating decoder fallback retained beside packed weights")
        scale_file=index[module+".weight_scale"]; a,b=scale["data_offsets"]
        with (root/scale_file).open("rb") as f:
            f.seek(parsed[scale_file][1]+a);data=f.read(b-a)
        import numpy as np
        scales=_float_array(data,scale["dtype"],scale["shape"])
        if not np.isfinite(scales).all() or not (scales > 0).all():
            raise ValueError("Quantization scales must be finite and positive")
        if dtype=="I32":
            shape_name=module+".weight_shape";desc=headers[index[shape_name]][shape_name]
            if desc["dtype"]!="I64" or desc["shape"]!=[2]:
                raise ValueError("Invalid packed original shape")
            with (root/index[shape_name]).open("rb") as f:
                f.seek(parsed[index[shape_name]][1]+desc["data_offsets"][0])
                if struct.unpack("<qq",f.read(16)) != (n,k):
                    raise ValueError("Packed original shape value mismatch")
    depth=(model_config.get("text_config") or model_config)["num_hidden_layers"]
    if coverage != {(i,p) for i in range(depth) for p in PROJECTIONS}:
        raise ValueError("Validated checkpoint decoder coverage incomplete")
    for item in proof["preserved_tensors"]:
        name=item["name"];filename=index[name];desc=headers[filename][name]
        if desc["dtype"]!=item["dtype"] or desc["shape"]!=item["shape"]:
            raise ValueError("Excluded tensor dtype/shape changed")
        with (root/filename).open("rb") as f:
            h=hashlib.sha256()
            a,b=desc["data_offsets"]
            for block in _read_chunks(f,parsed[filename][1]+a,b-a,1024*1024):h.update(block)
        if h.hexdigest()!=item["sha256"]:
            raise ValueError("Excluded tensor source bytes changed")
    return {"checkpoint_path": str(root), "model_ref":proof["model_ref"], "source_revision":proof["source_revision"],
            "mode":proof["mode"], "bits":proof["bits"], "format":proof["format"], "recipe_sha256":RECIPE_SHA256,
            "validation":{"status":"PASS","integer_weight_tensors":len(proof["quantized_tensors"]),
                          "preserved_tensors":len(proof["preserved_tensors"]),"file_checksums_verified":True,
                          "peak_float_block_bytes":proof["peak_float_block_bytes"]}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",required=True);parser.add_argument("--revision",required=True)
    parser.add_argument("--mode",choices=["int8","int4"],required=True)
    parser.add_argument("--output-dir");parser.add_argument("--chunk-bytes",type=int,default=64*1024*1024)
    args=parser.parse_args();_identity(args.model,args.revision,args.mode)
    for package,pin in {"vllm":"0.31.0","compressed-tensors":"0.17.0","torch":"2.13.0","transformers":"5.17.0"}.items():
        if importlib.metadata.version(package).split("+")[0] != pin:
            raise RuntimeError("Quantization runtime package pin mismatch: "+package)
    from huggingface_hub import snapshot_download
    snapshot=snapshot_download(args.model,revision=args.revision,allow_patterns=["*.safetensors","*.json","*.txt","*.model","*.jinja"])
    if Path(snapshot).name != args.revision:
        raise RuntimeError("Downloaded snapshot revision mismatch")
    output=args.output_dir or str(Path.home()/".cache/aig-quantized"/args.model.replace("/","--")/args.revision/(args.mode+"-"+RECIPE_SHA256[:12]))
    result=prepare_checkpoint(snapshot,args.model,args.revision,args.mode,output,args.chunk_bytes)
    print(json.dumps(result),flush=True)


if __name__ == "__main__":
    main()
