"""Заглушка PortfolioContext."""

from __future__ import annotations


class PortfolioContext:
    def __init__(self, **data):
        self.data = data

    @classmethod
    def model_validate(cls, data):
        return cls(**data)
