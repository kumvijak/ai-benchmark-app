"""Isolated tiny checkpoints exercise actual serialized integer bytes and failures."""
import hashlib
import json
from pathlib import Path
import struct
import re
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from app.quantization import (
    IGNORE_ALIASES, PROJECTIONS, RECIPE_SHA256, SUPPORTED_MODELS, _bf16_encode, _float_array,
    _header, _ShardWriter, build_prepare_command, prepare_checkpoint,
    quantize_rows, validate_checkpoint,
)


class QuantizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def checkpoint(self, model="Qwen/Qwen3-4B-Base", bad=False, missing=False, projection_rows=64):
        root = self.root / model.replace("/", "--")
        root.mkdir()
        is_vl = model == "Qwen/Qwen3-VL-8B-Instruct"
        prefix = "model.language_model.layers.0." if is_vl else "model.layers.0."
        config = {"model_type": SUPPORTED_MODELS[model], "num_hidden_layers": 1}
        if is_vl:
            config["text_config"] = {"num_hidden_layers": 1}
        (root / "config.json").write_text(json.dumps(config))
        (root / "tokenizer_config.json").write_text('{"tokenizer_class":"test"}')
        rng = np.random.default_rng(42)
        tensors = {}
        for p in sorted(PROJECTIONS):
            if missing and p == "mlp.up_proj":
                continue
            w = rng.standard_normal((projection_rows, 256), dtype=np.float32)
            if bad and p == "mlp.up_proj":
                w[0, 0] = np.nan
            tensors[prefix + p + ".weight"] = ("BF16", [projection_rows, 256], _bf16_encode(w).tobytes())
        tensors[prefix + "input_layernorm.weight"] = ("F32", [64], np.ones(64, dtype="<f4").tobytes())
        tensors["model.embed_tokens.weight"] = ("BF16", [64, 256], _bf16_encode(rng.standard_normal((64,256))).tobytes())
        tensors["lm_head.weight"] = ("BF16", [64,256], _bf16_encode(rng.standard_normal((64,256))).tobytes())
        tensors["model.rotary.inv_freq"] = ("F32", [], struct.pack("<f", 1.0))
        if is_vl:
            tensors["model.visual.blocks.0.attn.qkv.weight"] = ("BF16", [64,256], _bf16_encode(rng.standard_normal((64,256))).tobytes())
        mapping = {}
        for i, part in enumerate((list(tensors)[:4], list(tensors)[4:]), 1):
            filename = f"model-0000{i}-of-00002.safetensors"
            writer = _ShardWriter(root / filename)
            for name in part:
                dtype, shape, data = tensors[name]
                writer.tensor(name, dtype, shape, [data])
                mapping[name] = filename
            writer.finish()
        (root / "model.safetensors.index.json").write_text(json.dumps({"weight_map": mapping}))
        return root, tensors

    def read(self, root, name):
        mapping = json.loads((root/"model.safetensors.index.json").read_text())["weight_map"]
        header, base = _header(root/mapping[name]); d = header[name]
        a, b = d["data_offsets"]
        with (root/mapping[name]).open("rb") as f:
            f.seek(base+a); data=f.read(b-a)
        return d, data

    def test_real_storage_roundtrip_and_exclusions_all_architectures(self):
        for model in SUPPORTED_MODELS:
            source, tensors = self.checkpoint(model)
            for mode in ("int8", "int4"):
                with self.subTest(model=model, mode=mode):
                    output = self.root / (model.replace("/", "--") + "-" + mode)
                    result = prepare_checkpoint(source, model, "a"*40, mode, output, 1024*1024)
                    self.assertEqual(result["validation"]["status"], "PASS")
                    self.assertEqual(result["validation"]["integer_weight_tensors"], 7)
                    self.assertLessEqual(result["validation"]["peak_float_block_bytes"], 1024*1024//8)
                    for name, (dtype, shape, raw) in tensors.items():
                        if ".layers.0." not in name or ".input_layernorm." in name:
                            desc, saved = self.read(output, name)
                            self.assertEqual((desc["dtype"],desc["shape"],saved),(dtype,shape,raw))
                            continue
                        module = name[:-7]
                        desc, packed = self.read(output, name if mode=="int8" else module+".weight_packed")
                        self.assertEqual(desc["dtype"], "I8" if mode=="int8" else "I32")
                        sd, scale_bytes = self.read(output, module+".weight_scale")
                        scales = _float_array(scale_bytes,sd["dtype"],sd["shape"])
                        if mode == "int8":
                            q = np.frombuffer(packed,dtype="i1").reshape(shape)
                            reconstructed = q.astype(np.float32)*scales
                            bound = scales*.501
                        else:
                            u = np.frombuffer(packed,dtype="<u4").reshape(shape[0],shape[1]//8)
                            q = np.stack([((u>>(4*i))&15).astype(np.int16)-8 for i in range(8)],axis=-1).reshape(shape)
                            reconstructed = (q.reshape(shape[0],shape[1]//128,128)*scales[:,:,None]).reshape(shape)
                            bound = np.repeat(scales,128,axis=1)*.55
                            self.assertEqual(len(packed),shape[0]*shape[1]//2)
                        original = _float_array(raw,dtype,shape)
                        self.assertTrue(np.all(np.abs(reconstructed-original)<=bound+1e-5))
                    # Cache reuse validates the bytes rather than trusting directory existence.
                    self.assertEqual(prepare_checkpoint(source,model,"a"*40,mode,output)["recipe_sha256"],RECIPE_SHA256)

    def test_offset_binary_packing_matches_compressed_tensors_golden(self):
        # CT pack_to_int32 offsets signed nibbles by8 and packs lowest-column first.
        w = np.tile(np.arange(-8,8,dtype=np.float32),(32,8))
        packed, scale = quantize_rows(w,4)
        scales = _float_array(scale,"BF16",[32,1])
        q = np.clip(np.rint(w/scales),-8,7).astype(np.int8)
        words = np.frombuffer(packed,dtype="<u4").reshape(32,16)
        expected = sum((int(q[0,i])+8)<<(4*i) for i in range(8))
        self.assertEqual(int(words[0,0]),expected)

    def test_nonfinite_and_incomplete_source_fail_without_publishing(self):
        for kind in ("nan","missing"):
            with self.subTest(kind=kind):
                if kind=="missing":
                    self.root=self.root/"second";self.root.mkdir()
                source,_=self.checkpoint(bad=kind=="nan",missing=kind=="missing")
                output=self.root/"out"
                with self.assertRaises(ValueError):prepare_checkpoint(source,"Qwen/Qwen3-4B-Base","a"*40,"int4",output)
                self.assertFalse(output.exists())
                self.assertFalse(list(self.root.glob("out.preparing-*")))

    def test_corrupt_serialized_bytes_rejected_on_reuse(self):
        source,_=self.checkpoint();out=self.root/"out"
        prepare_checkpoint(source,"Qwen/Qwen3-4B-Base","a"*40,"int8",out)
        path=next(out.glob("*.safetensors"))
        with path.open("r+b") as f:f.seek(-1,2);f.write(b"\xff")
        with self.assertRaisesRegex(ValueError,"checksum"):validate_checkpoint(out)

    def test_large_matrices_are_split_by_fixed_memory_budget(self):
        source,_=self.checkpoint(projection_rows=512);sizes=[]
        def tracked(block,bits):
            sizes.append(block.nbytes)
            return quantize_rows(block,bits)
        with patch("app.quantization.quantize_rows",side_effect=tracked):
            prepare_checkpoint(source,"Qwen/Qwen3-4B-Base","a"*40,"int4",self.root/"out",1024*1024)
        self.assertGreater(len(sizes),7)
        self.assertLessEqual(max(sizes),1024*1024//8)

    def test_zero_scales_fail_even_with_updated_file_hash(self):
        source,_=self.checkpoint();out=self.root/"out"
        prepare_checkpoint(source,"Qwen/Qwen3-4B-Base","a"*40,"int4",out)
        index=json.loads((out/"model.safetensors.index.json").read_text())["weight_map"]
        name=next(n for n in index if n.endswith(".weight_scale"));path=out/index[name]
        header,base=_header(path);a,b=header[name]["data_offsets"]
        with path.open("r+b") as f:f.seek(base+a);f.write(b"\0"*(b-a))
        manifest=out/"quantization-manifest.json";proof=json.loads(manifest.read_text())
        proof["file_sha256"][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(proof))
        with self.assertRaisesRegex(ValueError,"finite and positive"):validate_checkpoint(out)

    def test_header_rejects_overlapping_and_truncated_offsets(self):
        path=self.root/"invalid.safetensors"
        desc={"x":{"dtype":"I8","shape":[2],"data_offsets":[0,2]},"y":{"dtype":"I8","shape":[2],"data_offsets":[1,3]}}
        header=json.dumps(desc).encode();path.write_bytes(struct.pack("<Q",len(header))+header+b"123")
        with self.assertRaises(ValueError):_header(path)
        path.write_bytes(struct.pack("<Q",999999))
        with self.assertRaises(ValueError):_header(path)

    def test_contract_rejects_unpinned_revision_and_unknown_model(self):
        with self.assertRaises(ValueError):build_prepare_command("Qwen/Qwen3-4B-Base","main","int8")
        with self.assertRaises(ValueError):build_prepare_command("fake/model","a"*40,"int8")
        command=build_prepare_command("Qwen/Qwen3-4B-Base","a"*40,"int4",output_dir="/tmp/a checkpoint")
        self.assertIn("--mode int4",command);self.assertIn("'/tmp/a checkpoint'",command)

    def test_ignore_aliases_cover_remapped_vision_without_decoder(self):
        for module in ["model.visual.blocks.0.attn.qkv", "visual.blocks.0.attn.qkv",
                       "model.vision_tower.encoder.layers.0.fc1", "lm_head", "language_model.lm_head"]:
            self.assertTrue(any(re.match(p[3:],module) for p in IGNORE_ALIASES))
        for module in ["model.layers.0.self_attn.q_proj", "language_model.model.layers.0.mlp.up_proj"]:
            self.assertFalse(any(re.match(p[3:],module) for p in IGNORE_ALIASES))


if __name__ == "__main__":
    unittest.main()
