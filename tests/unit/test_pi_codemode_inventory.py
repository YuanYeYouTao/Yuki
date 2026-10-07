"""P00: declaration coverage cannot silently omit an unknown tool."""

import pytest
from scripts.export_pi_codemode_inventory import export_inventory, inventory_rows

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog
from qq_ai_bot.domain.messages import ChatTool


async def test_inventory_maps_actual_frozen_contract_without_executing_tools():
    inventory = await export_inventory()
    rows = inventory["tools"]
    assert {row["model_name"] for row in rows} == {
        tool["name"] for tool in inventory["frozen_definitions"]
    }
    assert {"core", "admin", "automation", "host_control"} <= {row["provider_id"] for row in rows}
    assert all(row["binding"] for row in rows)
    assert all(row["acceptance_status"] == "not_run" for row in rows)
    assert inventory["external_inventory"]["production_manifest"].startswith("not_collected")


def test_inventory_rejects_unmapped_declaration():
    with pytest.raises(ValueError, match="unmapped_manifest_tool:unbound"):
        inventory_rows(
            (ChatTool(name="unbound", description="", parameters={"type": "object"}),),
            UnifiedToolCatalog((), (), "fixture"),
        )
