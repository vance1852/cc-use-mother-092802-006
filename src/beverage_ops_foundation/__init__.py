"""技能赛训协作基础服务的服务端基础包。"""

from .disposal_service import DisposalService
from .service import DomainService

__all__ = ["DomainService", "DisposalService"]
