from abc import ABC, abstractmethod


class ModernizerPlugin(ABC):
    """
    Abstract base class that every modernizer plugin must implement.
    This is the 'contract' - any plugin following this shape can be
    plugged into the registry and dropdown without special-casing.
    """

    @property
    @abstractmethod
    def source_type(self) -> str:
        """The input type this plugin handles, e.g. 'java'"""
        pass

    @property
    @abstractmethod
    def target_type(self) -> str:
        """What this plugin converts TO, e.g. 'python'"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable label, shown in the dropdown"""
        pass

    @abstractmethod
    def convert(self, content: str) -> str:
        """
        Takes raw source code as a string, returns modernized code as a string.
        Each plugin implements its own conversion logic here.
        """
        pass