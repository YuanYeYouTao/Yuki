"""Version 0076 indexes for bounded reads of original Work relations."""

import sqlalchemy as sa

from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs
from qq_ai_bot.runtime.work_wait_schema import waits

sa.Index("ix_work_inputs_work_id", inputs.c.work_id, inputs.c.id)
sa.Index("ix_work_effects_work_updated", effects.c.work_id, effects.c.updated, effects.c.effect_key)
sa.Index("ix_work_children_root", children.c.root_id, children.c.work_id)
sa.Index("ix_work_waits_work_created", waits.c.work_id, waits.c.created, waits.c.id)
