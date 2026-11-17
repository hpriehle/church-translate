"""The Wolof SpeechT5 voice on onnxruntime: no torch, no transformers (Windows, and any CPU-only computer).

The same model as the Apple Silicon Mac's torch voice (bilalfaye/speecht5_tts-wolof + microsoft/speecht5_hifigan,
both MIT), exported by scripts/models/export_speecht5_onnx.py into three graphs:

  encoder.onnx   input_ids [1, T] -> cross_k, cross_v [layers, heads, T, 64]: the text encoder, plus each decoder
                 layer's cross-attention keys/values (computed once per sentence)
  decoder.onnx   one step: the last mel frame, the prenet's dropout masks, the position, the speaker profile, the
                 cross keys/values and the self-attention cache -> 2 mel frames, stop probabilities, the new cache
  vocoder.onnx   mel [1, N, 80] -> waveform [1, N * 256] at 16 kHz: the postnet and HiFi-GAN
  tokens.json    the character vocabulary (SentencePiece char model, read without sentencepiece)

The loop below is transformers' SpeechT5 `_generate_speech` for one sentence. SpeechT5 keeps its prenet dropout on
when speaking (§2.2 of the Tacotron 2 paper); the masks are drawn here and passed in, so a fixed seed gives the
same speech as the torch model with the same masks (the export's --check compares them).
"""
from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import numpy as np

SR = 16000


class Tokenizer:
    """SpeechT5's SentencePiece character model: NFKC, words joined by "▁", one piece per character, then </s>."""

    def __init__(self, pieces: list[str], unk: int = 0, eos: int = 2):
        self.ids = {p: i for i, p in enumerate(pieces)}
        self.unk, self.eos = unk, eos

    def __call__(self, text: str) -> list[int]:
        out = []
        for word in unicodedata.normalize("NFKC", text).split():
            out += [self.ids["▁"]] + [self.ids.get(c, self.unk) for c in word]
        return out + [self.eos]


def _session(path: Path, threads: int):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    # The decoder and the vocoder run at the same time (see speak_all): no busy-waiting threads taking the cores.
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])


class SpeechT5Onnx:
    def __init__(self, folder: Path, threads: int = 0, decoder_threads: int = 0):
        """The decoder takes one small step at a time: one or two threads run it fastest (more only add overhead).
        The vocoder is heavy convolutions and gets the rest of the cores."""
        folder = Path(folder)
        cfg = json.loads((folder / "config.json").read_text())
        cores = os.cpu_count() or 1
        decoder_threads = decoder_threads or (2 if cores >= 6 else 1)
        threads = threads or max(1, min(4, cores - decoder_threads))
        self.enc = _session(folder / cfg["encoder"], decoder_threads)
        self.dec = _session(folder / cfg["decoder"], decoder_threads)
        self.voc = _session(folder / cfg["vocoder"], threads)
        tok = json.loads((folder / "tokens.json").read_text())
        self.tokenize = Tokenizer(tok["pieces"], tok.get("unk", 0), tok.get("eos", 2))
        self.layers, self.heads, self.head_dim = cfg["layers"], cfg["heads"], cfg["head_dim"]
        self.mels, self.prenet_units, self.dropout = cfg["mels"], cfg["prenet_units"], cfg["prenet_dropout"]
        self.reduction = cfg["reduction"]
        self.max_positions = cfg["max_positions"]
        self.sr = cfg.get("sample_rate", SR)

    def masks(self, rng: np.random.Generator) -> np.ndarray:
        # As transformers does it: torch.bernoulli(x, p) keeps a unit with probability p, scaled by 1 / (1 - p).
        p = self.dropout
        return ((rng.random((2, self.prenet_units)) < p) / (1.0 - p)).astype(np.float32)

    def mel(self, text: str, speaker: np.ndarray, threshold: float = 0.5, maxlenratio: float = 20.0,
            minlenratio: float = 0.0, rng: np.random.Generator | None = None, masks=None) -> np.ndarray:
        """The mel spectrogram [frames, 80] before the postnet. `masks(step)` overrides the dropout (for tests)."""
        rng = rng or np.random.default_rng()
        ids = np.array([self.tokenize(text)], dtype=np.int64)
        cross_k, cross_v = self.enc.run(None, {"input_ids": ids})
        t = ids.shape[1]
        maxlen = min(int(t * maxlenratio / self.reduction), self.max_positions)
        minlen = int(t * minlenratio / self.reduction)
        spk = np.asarray(speaker, dtype=np.float32).reshape(1, -1)
        prev = np.zeros((1, 1, self.mels), np.float32)
        past_k = np.zeros((self.layers, self.heads, 0, self.head_dim), np.float32)
        past_v = past_k
        frames = []
        idx = 0
        while True:
            idx += 1
            m = masks(idx) if masks else self.masks(rng)
            spec, prob, past_k, past_v = self.dec.run(None, {
                "prev_mel": prev, "dropout_masks": m, "position": np.array([idx - 1], np.int64),
                "speaker": spk, "cross_k": cross_k, "cross_v": cross_v, "past_k": past_k, "past_v": past_v})
            frames.append(spec[0])
            prev = spec[:, -1:, :]
            if idx < minlen:
                continue
            if idx >= maxlen or float(prob.sum()) >= threshold:
                break
        return np.concatenate(frames, axis=0)

    def vocode(self, mel: np.ndarray) -> np.ndarray:
        return self.voc.run(None, {"mel": mel[None].astype(np.float32)})[0][0]

    def speak(self, text: str, speaker: np.ndarray, **kw) -> np.ndarray:
        """float32 waveform at self.sr for one sentence."""
        return self.vocode(self.mel(text, speaker, **kw))

    def speak_all(self, sentences: list[str], speaker: np.ndarray, **kw) -> list[np.ndarray]:
        """Several sentences, the vocoder turning one into sound while the decoder writes the next."""
        from concurrent.futures import ThreadPoolExecutor

        out = []
        with ThreadPoolExecutor(1) as pool:
            pending = None
            for s in sentences:
                mel = self.mel(s, speaker, **kw)
                if pending is not None:
                    out.append(pending.result())
                pending = pool.submit(self.vocode, mel)
            if pending is not None:
                out.append(pending.result())
        return out
