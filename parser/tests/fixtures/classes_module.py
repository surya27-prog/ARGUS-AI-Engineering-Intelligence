"""Classes, inheritance, decorators and methods."""

from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class Base:
    """A plain base class."""

    def describe(self) -> str:
        return "base"


@dataclass(frozen=True)
class Record(Base):
    """Inherits from Base and carries a decorator with arguments."""

    name: str
    status: Status = Status.ACTIVE

    @property
    def label(self) -> str:
        return f"{self.name} ({self.status})"

    @staticmethod
    def build(name: str) -> "Record":
        return Record(name=name)

    async def refresh(self, *, force: bool = False) -> None:
        self.describe()

    class Meta:
        ordering = ("name",)
