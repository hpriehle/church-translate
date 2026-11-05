"""Split the exported requirements into what the app ships and what only the Wolof voice needs.

  split_requirements.py uv.lock requirements.txt app.txt wolof.txt

torch and transformers (about 800 MB) serve only the Wolof voice, so they download with it instead of
sitting in every copy of the app. A package goes with Wolof only if nothing else reaches it in uv.lock.
"""
import re
import sys
import tomllib

WOLOF_ROOTS = {"torch", "transformers", "sentencepiece"}


def wolof_only(lock: dict) -> set[str]:
    deps: dict[str, set[str]] = {}
    root = ""
    for p in lock["package"]:
        deps.setdefault(p["name"], set()).update(d["name"] for d in p.get("dependencies", []))
        if "virtual" in p.get("source", {}) or "editable" in p.get("source", {}):
            root = p["name"]

    def reach(skip: set[str]) -> set[str]:
        seen: set[str] = set()
        stack = [root]
        while stack:
            name = stack.pop()
            if name not in seen:
                seen.add(name)
                stack += [d for d in deps.get(name, ()) if d not in skip]
        return seen

    return reach(set()) - reach(WOLOF_ROOTS)


def split(lock: dict, lines: list[str]) -> tuple[list[str], list[str]]:
    only = wolof_only(lock)
    app, wolof = [], []
    for line in lines:
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if m:
            (wolof if re.sub(r"[-_.]+", "-", m[1]).lower() in only else app).append(line.rstrip())
    return app, wolof


if __name__ == "__main__":
    lock_path, req_path, app_path, wolof_path = sys.argv[1:5]
    with open(lock_path, "rb") as f:
        lock = tomllib.load(f)
    with open(req_path) as f:
        app, wolof = split(lock, f.readlines())
    if not {"torch", "transformers"} <= {line.split("==")[0] for line in wolof}:
        sys.exit("split_requirements: torch and transformers should be Wolof-only")
    with open(app_path, "w") as f:
        f.write("\n".join(app) + "\n")
    with open(wolof_path, "w") as f:
        f.write("\n".join(wolof) + "\n")
