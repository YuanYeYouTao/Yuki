"""Valid structured auxiliary responses for execution-focused tests."""

import json


def summary_json(source, text="Continue the original task."):
    if isinstance(source, str):
        source = json.loads(source)
    directives = [
        {"text": item["text"], "refs": item["refs"]}
        for item in source["task_material"].get("directives", [])
    ]
    directives.extend(
        {"text": item["text"], "refs": [f"input:{item['input_id']}"]}
        for item in source["task_inputs"]
    )
    return json.dumps(
        {
            "version": 1,
            "task_directives": directives,
            "superseded_directives": [],
            "input_dispositions": [
                {
                    "input_ref": f"input:{item['input_id']}",
                    "kind": "directive",
                    "reason": "Explicit requirement.",
                }
                for item in source["task_inputs"]
            ],
            "completed": [],
            "pending": [{"text": text, "refs": ["goal"]}],
            "failures": [],
            "artifacts": [],
            "next_steps": [],
        }
    )


async def session_summary(session, text="Continue the original task."):
    return summary_json(await session.summary_source(), text)
