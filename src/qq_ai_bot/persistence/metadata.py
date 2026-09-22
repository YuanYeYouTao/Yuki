"""Aggregate every SQLAlchemy model into the deployment metadata.

Domain-owned tables live beside their repositories.  Importing this module is
the single supported way for Alembic and test schema creation to discover all
of them without turning :mod:`qq_ai_bot.persistence.models` into a monolith.
"""

# These imports are intentionally side-effectful: defining each mapped class
# registers its table on ``Base.metadata``.
from qq_ai_bot.conversation import autonomy_db_models as _autonomy_db_models  # noqa: F401
from qq_ai_bot.conversation import (  # noqa: F401
    canonical_db_models as _canonical_conversation_db_models,
)
from qq_ai_bot.conversation import projection_models as _projection_models  # noqa: F401
from qq_ai_bot.emoji import db_models as _emoji_db_models  # noqa: F401
from qq_ai_bot.identity import db_models as _identity_db_models  # noqa: F401
from qq_ai_bot.memory.dream import db_models as _memory_dream_db_models  # noqa: F401
from qq_ai_bot.memory.self_reflection import db_models as _self_reflection_db_models  # noqa: F401
from qq_ai_bot.model_runtime import db_models as _model_runtime_db_models  # noqa: F401
from qq_ai_bot.persistence.models import Base
from qq_ai_bot.plugin_host import db_models as _plugin_db_models  # noqa: F401
from qq_ai_bot.runtime import automation_budget_schema as _automation_budget_schema  # noqa: F401
from qq_ai_bot.runtime import work_schema_v1 as _work_schema_v1  # noqa: F401
from qq_ai_bot.sandbox import db_models as _sandbox_db_models  # noqa: F401
from qq_ai_bot.social import db_models as _social_db_models  # noqa: F401
from qq_ai_bot.speech import db_models as _speech_db_models  # noqa: F401

__all__ = ["Base"]
