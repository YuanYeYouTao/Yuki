"""Attach observed passing test nodes to the acceptance matrix after a full run."""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from qq_ai_bot.runtime.work_control import WORK_CONTROL_NAMES

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/architecture/pi-codemode-acceptance.json"
EVIDENCE = ROOT / "docs/architecture/pi-codemode-evidence"

GROUPS = {
    "T01": ["unit/test_codemode_authority_parity", "unit/test_codemode_api_projection"],
    "T02": ["unit/test_codemode_mcp_authority", "unit/test_tool_kernel_mcp"],
    "T03": ["unit/test_codemode_plugin_revocation", "unit/test_main_agent_entrypoints"],
    "T04": ["integration/test_codemode_control_gate", "unit/test_runtime_work"],
    "T05": ["integration/test_codemode_worker_entrypoint", "unit/test_subagents"],
    "T06": ["unit/test_automation_runtime"],
    "T07": ["integration/test_codemode_memory_authority", "unit/test_agent_receipt_loop"],
    "T08": ["unit/test_sandbox", "integration/test_codemode_worker_entrypoint"],
    "T09": ["unit/test_workspace", "unit/test_control_workspace"],
    "T11": ["integration/test_codemode_social_receipts"],
    "D04": ["unit/test_social", "unit/test_social_connection_boundary"],
    "D06": ["unit/test_work_reporting_runner"],
    "D07": ["integration/test_codemode_social_receipts", "unit/test_main_agent_entrypoints"],
    "F01": [
        "integration/test_invocation_crash_windows",
        "integration/test_invocation_process_crash",
    ],
    "F02": [
        "integration/test_invocation_crash_windows",
        "integration/test_invocation_process_crash",
    ],
    "F03": [
        "integration/test_invocation_crash_windows",
        "integration/test_codemode_social_receipts",
    ],
    "F04": [
        "integration/test_invocation_crash_windows",
        "integration/test_invocation_process_crash",
    ],
    "F05": ["integration/test_invocation_process_crash", "integration/test_codemode_composition"],
    "F06": ["integration/test_codemode_control_gate", "integration/test_codemode_worker"],
    "F07": [
        "integration/test_invocation_crash_windows",
        "integration/test_codemode_resource_policy",
    ],
    "F08": [
        "integration/test_codemode_resource_policy",
        "unit/test_codemode_plugin_revocation",
        "unit/test_codemode_mcp_authority",
        "unit/test_work_protocol_continuity",
    ],
    "C01": ["integration/test_codemode_composition", "unit/test_agent_core_differences"],
    "C02": ["integration/test_codemode_control_gate", "integration/test_codemode_composition"],
    "C03": ["integration/test_codemode_runner"],
    "C04": ["integration/test_codemode_control_gate", "unit/test_work_protocol_continuity"],
    "C05": ["integration/test_codemode_control_gate", "integration/test_codemode_composition"],
    "C07": ["integration/test_codemode_control_gate"],
    "C08": ["integration/test_codemode_worker_entrypoint", "unit/test_subagents"],
    "B01": ["integration/test_codemode_runner", "integration/test_codemode_provider_wire"],
    "B02": ["unit/test_execution_trace", "unit/test_agent_core_differences"],
    "B03": ["integration/test_invocation_process_crash", "integration/test_codemode_composition"],
    "B04": ["unit/test_subagents"],
    "B05": ["integration/test_codemode_composition", "unit/test_subagents"],
    "B06": ["unit/test_model_telemetry_failures", "unit/test_provider_protocols"],
    "B07": ["integration/test_codemode_resource_policy", "integration/test_codemode_worker"],
    "B08": ["unit/test_model_telemetry_failures", "unit/test_execution_trace"],
    "X01": ["unit/test_codemode_contracts", "integration/test_codemode_worker"],
    "X02": ["unit/test_pi_codemode_inventory", "unit/test_codemode_api_projection"],
    "X03": ["unit/test_invocation_identity", "integration/test_codemode_social_receipts"],
    "X04": ["integration/test_invocation_crash_windows", "unit/test_runtime_work"],
    "X05": ["integration/test_codemode_trace", "unit/test_execution_trace"],
    "X06": [
        "unit/test_agent_core_loop",
        "unit/test_agent_core_differential",
        "unit/test_main_agent_entrypoints",
    ],
    "X08": [
        "integration/test_codemode_backup_recovery",
        "integration/test_codemode_worker",
        "integration/test_codemode_resource_policy",
    ],
    "X09": [
        "unit/test_codemode_authority_parity",
        "integration/test_codemode_memory_authority",
        "integration/test_codemode_automation_entrypoint",
    ],
    "X12": ["unit/test_main_agent_entrypoints", "integration/test_codemode_chat_entrypoint"],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    args = parser.parse_args()
    xml_bytes = args.junit.read_bytes()
    root = ET.fromstring(xml_bytes)
    cases = list(root.iter("testcase"))
    assert cases and not any(
        c.find("failure") is not None or c.find("error") is not None for c in cases
    )
    passed = sorted(
        c.attrib["classname"].replace(".", "/") + ".py::" + c.attrib["name"]
        for c in cases
        if c.find("skipped") is None
    )
    skipped = [
        c.attrib["classname"] + "::" + c.attrib["name"]
        for c in cases
        if c.find("skipped") is not None
    ]
    matrix = json.loads(MATRIX.read_text())
    for item in matrix["items"]:
        targets = ["tests/" + name + ".py" for name in GROUPS.get(item["id"], [])]
        if not targets:
            targets = [
                e.split(".py", 1)[0] + ".py"
                for e in item["evidence"]
                if e.startswith("tests/") and ".py" in e
            ]
        nodes = [node for node in passed if node.split("::")[0] in targets]
        if item["id"] == "X11":
            audit = json.loads((ROOT / "vendor/monty/THIRD_PARTY_NOTICES.json").read_text())
            packaging_path = EVIDENCE / "final-container-packaging.json"
            packaging = json.loads(packaging_path.read_text()) if packaging_path.is_file() else {}
            profiles = packaging.get("profiles", [])
            probe_sha256 = hashlib.sha256(
                (ROOT / "scripts/verify_monty_packaging.py").read_bytes()
            ).hexdigest()
            images_current = (
                packaging.get("probe_sha256") == probe_sha256
                and all(
                    packaging.get("source_sha256", {}).get(path)
                    == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                    for path in (
                        "Dockerfile",
                        "deploy/codemode/Dockerfile.validation",
                        "scripts/export_monty_notices.py",
                    )
                )
                and {p["role"] for p in profiles} == {"application", "validation"}
                and all(
                    p["license_text_audit_complete"]
                    and p["license_text_gaps"] == 0
                    and p["packaged_pi_license"] is False
                    for p in profiles
                )
            )
            item["verdict"] = (
                "passed" if audit["license_text_audit_complete"] and images_current else "partial"
            )
            item["scope"] = (
                f"{len(audit['license_text_gaps'])} license text inventory gaps; "
                f"{len(audit.get('upstream_notice_omissions', []))} original upstream notice "
                "omissions retained separately; Pi is a design reference; "
                f"updated image verification {'observed' if images_current else 'unavailable'}; "
                "image publication/deployment unperformed"
            )
        else:
            assert nodes, f"no observed test evidence for {item['id']}"
            item["verdict"] = "passed"
            item["scope"] = (
                "offline implementation and isolated downstreams; external status is separate"
            )
        item["p10_observed_nodes"] = nodes
        item["p10_junit_sha256"] = hashlib.sha256(xml_bytes).hexdigest()
    matrix["p10_run"] = {
        "passed": len(passed),
        "skipped": skipped,
        "junit_sha256": hashlib.sha256(xml_bytes).hexdigest(),
        "scope": "real native Monty binding/worker; no production or paid Provider calls",
    }
    MATRIX.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n")
    inventory = json.loads(
        (ROOT / "docs/architecture/pi-codemode-capability-inventory.json").read_text()
    )
    coverage = []
    for tool in inventory["tools"]:
        name = tool["model_name"]
        denial = [
            n for n in passed if "test_child_refusal_is_identical_to_direct[" + name + "]" in n
        ]
        common = [n for n in passed if "test_every_inventory_tool_has_one_exact_wrapper" in n]
        controls = [
            n for n in passed if n.startswith("tests/integration/test_codemode_control_gate.py::")
        ]
        code = [n for n in passed if n.startswith("tests/integration/test_codemode_runner.py::")]
        assert denial or name == "execute_code" or name in WORK_CONTROL_NAMES, (
            f"unmapped fixed declaration: {name}"
        )
        coverage.append(
            {
                "model_name": name,
                "canonical_name": tool["canonical_name"],
                "binding": tool["binding"],
                "input_schema": tool["input_schema"],
                "output_schema": tool["output_schema"],
                "wrapper_projection_nodes": common,
                "direct_child_refusal_nodes": denial,
                "control_or_code_nodes": code
                if name == "execute_code"
                else controls
                if not denial
                else [],
                "scope": "declaration/schema/execution-path/refusal closure; "
                "original domain behavior remains in full suite",
            }
        )
    (EVIDENCE / "p10-tool-coverage.json").write_text(
        json.dumps(
            {
                "tools": coverage,
                "fixed_count": len(coverage),
                "external_inventory": inventory["external_inventory"],
                "junit_sha256": hashlib.sha256(xml_bytes).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
