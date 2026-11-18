"""Report parentUuid tree shape of a Claude transcript: branches = parents with >1 conversational child."""
import json, sys, collections
nodes, kids = {}, collections.defaultdict(list)
for l in open(sys.argv[1]):
    o = json.loads(l)
    if o.get("uuid") and o.get("type") in ("user", "assistant", "system", "attachment"):
        nodes[o["uuid"]] = o
        kids[o.get("parentUuid")].append(o["uuid"])
branches = {p: c for p, c in kids.items() if len(c) > 1}
entry = collections.Counter(o.get("entrypoint") for o in nodes.values())
print(json.dumps({"nodes": len(nodes), "roots": len(kids.get(None, [])), "branch_points": len(branches),
                  "branch_children_entrypoints": [[nodes[c].get("entrypoint") for c in cs] for cs in branches.values()],
                  "entrypoints": entry}))
