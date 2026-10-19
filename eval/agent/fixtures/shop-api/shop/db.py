"""개발용 메모리 저장소. 운영에서는 같은 인터페이스로 MySQL을 쓴다."""

import itertools
from dataclasses import dataclass, field


@dataclass
class Table:
    rows: dict[int, dict] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def insert(self, row: dict) -> dict:
        row = {**row, "id": next(self._ids)}
        self.rows[row["id"]] = row
        return row

    def get(self, row_id: int) -> dict | None:
        return self.rows.get(row_id)

    def update(self, row_id: int, **values) -> dict:
        self.rows[row_id].update(values)
        return self.rows[row_id]

    def all(self) -> list[dict]:
        return list(self.rows.values())


class Database:
    def __init__(self) -> None:
        self.products = Table()
        self.orders = Table()
        self.payments = Table()


db = Database()
