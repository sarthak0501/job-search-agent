from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Dict


class JobSource(ABC):
    @abstractmethod
    def fetch(self) -> List[Dict]:
        """Return a list of job dicts with keys:
        external_id, source, company, title, location, url, description
        """
