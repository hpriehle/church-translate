"""Piper voice synthesis speed on a CPU server: seconds of work per second of speech, per voice and thread count.

  python scripts/bench/piper_speed.py --voices DIR --threads 1 2 4
DIR holds <voice>.onnx + <voice>.onnx.json (e.g. ru_RU-denis-medium, lv_LV-aivars-medium) from rhasspy/piper-voices.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import onnxruntime as ort
from piper import PiperVoice

TEXT = {
    "ru": ["Братья и сёстры, сегодня я хочу поговорить о вере.",
           "Когда мы молимся, Господь слышит нас, даже если ответ приходит не сразу.",
           "Давайте помнить о тех, кто нуждается в нашей помощи, и служить им с любовью.",
           "Спаситель учил нас прощать друг друга, как Он прощает нас."],
    "lv": ["Brāļi un māsas, šodien es vēlos runāt par ticību.",
           "Kad mēs lūdzam, Tas Kungs mūs dzird, pat ja atbilde nenāk uzreiz.",
           "Atcerēsimies tos, kam vajadzīga mūsu palīdzība, un kalposim viņiem ar mīlestību.",
           "Glābējs mūs mācīja piedot cits citam, kā Viņš piedod mums."],
    "en": ["Brothers and sisters, today I want to speak about faith.",
           "When we pray, the Lord hears us, even when the answer does not come at once.",
           "Let us remember those who need our help, and serve them with love.",
           "The Savior taught us to forgive one another, as He forgives us."],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", required=True)
    ap.add_argument("--threads", type=int, nargs="+", default=[1, 2, 4])
    ap.add_argument("--rounds", type=int, default=5)
    a = ap.parse_args()
    out = []
    for onnx in sorted(Path(a.voices).glob("*.onnx")):
        lang = onnx.name[:2]
        voice = PiperVoice.load(str(onnx))
        for t in a.threads:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = t
            opts.inter_op_num_threads = 1
            voice.session = ort.InferenceSession(str(onnx), sess_options=opts, providers=["CPUExecutionProvider"])
            list(voice.synthesize(TEXT[lang][0]))   # warm-up
            work = speech = 0.0
            per = []
            for _ in range(a.rounds):
                for s in TEXT[lang]:
                    t0 = time.monotonic()
                    chunks = list(voice.synthesize(s))
                    dt = time.monotonic() - t0
                    sec = sum(len(c.audio_int16_array) for c in chunks) / chunks[0].sample_rate
                    work += dt
                    speech += sec
                    per.append(dt)
            row = {"voice": onnx.stem, "threads": t, "rtf": round(work / speech, 3),
                   "sentence_ms_mean": round(1000 * sum(per) / len(per)), "sentence_ms_max": round(1000 * max(per))}
            print(json.dumps(row), flush=True)
            out.append(row)
    Path("piper_results.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
