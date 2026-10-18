"""Shared management application operations, independent of HTTP and chat."""

from yuki.admin.config_files import ConfigFileService
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.operations.base import CommandExecutor
from yuki.admin.operations.config import ConfigOperations
from yuki.admin.operations.groups import GroupOperations
from yuki.admin.operations.identity import IdentityOperations
from yuki.admin.operations.models import ModelOperations
from yuki.admin.operations.plugins import PluginOperations
from yuki.admin.operations.work import WorkOperations
from yuki.client_api.identity import ClientIdentityService
from yuki.persistence.database import Database
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import GroupSettingsRepository
from yuki.plugin_host.manager import PluginManager


class AdminOperations:
    def __init__(
        self,
        database: Database,
        plugins: PluginManager,
        files: ConfigFileService,
        *,
        identities: ClientIdentityService,
        people: PeopleRepository,
        config: RuntimeConfigService,
        groups: GroupSettingsRepository,
    ) -> None:
        self.executor = CommandExecutor(database)
        self.config = ConfigOperations(self.executor, config)
        self.group = GroupOperations(self.executor, groups)
        self.identity = IdentityOperations(self.executor, identities, people)
        self.plugin = PluginOperations(self.executor, plugins)
        self.model = ModelOperations(self.executor, files)
        self.work = WorkOperations(self.executor)
